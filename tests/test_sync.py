from typing import Any

import pytest
from conftest import DEFAULT_URI  # type: ignore
from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    create_checkpoint,
    empty_checkpoint,
)
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.checkpoint.serde.types import TASKS


class TestPyMySQLSaver:
    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        # objects for test setup
        self.config_1: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-1",
                # for backwards compatibility testing
                "thread_ts": "1",
                "checkpoint_ns": "",
            }
        }
        self.config_2: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-2",
                "checkpoint_id": "2",
                "checkpoint_ns": "",
            }
        }
        self.config_3: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-2",
                "checkpoint_id": "2-inner",
                "checkpoint_ns": "inner",
            }
        }

        self.chkpnt_1: Checkpoint = empty_checkpoint()
        self.chkpnt_2: Checkpoint = create_checkpoint(self.chkpnt_1, {}, 1)
        self.chkpnt_3: Checkpoint = empty_checkpoint()

        self.metadata_1: CheckpointMetadata = {
            "source": "input",
            "step": 2,
            "writes": {},
            "score": 1,
        }
        self.metadata_2: CheckpointMetadata = {
            "source": "loop",
            "step": 1,
            "writes": {"foo": "bar"},
            "score": None,
        }
        self.metadata_3: CheckpointMetadata = {}
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            saver.setup()

    def test_search(self) -> None:
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            # save checkpoints
            saver.put(self.config_1, self.chkpnt_1, self.metadata_1, {})
            saver.put(self.config_2, self.chkpnt_2, self.metadata_2, {})
            saver.put(self.config_3, self.chkpnt_3, self.metadata_3, {})

            # call method / assertions
            query_1 = {"source": "input"}  # search by 1 key
            query_2 = {
                "step": 1,
                "writes": {"foo": "bar"},
            }  # search by multiple keys
            query_3: dict[str, Any] = {}  # search by no keys, return all checkpoints
            query_4 = {"source": "update", "step": 1}  # no match

            search_results_1 = list(saver.list(None, filter=query_1))
            assert len(search_results_1) == 1
            assert search_results_1[0].metadata == self.metadata_1

            search_results_2 = list(saver.list(None, filter=query_2))
            assert len(search_results_2) == 1
            assert search_results_2[0].metadata == self.metadata_2

            search_results_3 = list(saver.list(None, filter=query_3))
            assert len(search_results_3) == 3

            search_results_4 = list(saver.list(None, filter=query_4))
            assert len(search_results_4) == 0

            # search by config (defaults to checkpoints across all namespaces)
            search_results_5 = list(
                saver.list({"configurable": {"thread_id": "thread-2"}})
            )
            assert len(search_results_5) == 2
            assert {
                search_results_5[0].config["configurable"]["checkpoint_ns"],
                search_results_5[1].config["configurable"]["checkpoint_ns"],
            } == {"", "inner"}

            # TODO: test before and limit params

    def test_null_chars(self) -> None:
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            config = saver.put(self.config_1, self.chkpnt_1, {"my_key": "\x00abc"}, {})
            assert saver.get_tuple(config).metadata["my_key"] == "abc"  # type: ignore
            assert (
                list(saver.list(None, filter={"my_key": "abc"}))[0].metadata["my_key"]  # type: ignore
                == "abc"
            )

    def test_write_and_read_pending_writes_and_sends(self) -> None:
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_id": "1",
                    "checkpoint_ns": "",
                }
            }
            chkpnt = create_checkpoint(self.chkpnt_1, {}, 1, id="1")

            saver.put(config, chkpnt, {}, {})
            saver.put_writes(config, [("w1", "w1v"), ("w2", "w2v")], "world")
            saver.put_writes(config, [(TASKS, "w3v")], "hello")

            result = next(saver.list({}))

            assert result.pending_writes == [
                ("hello", TASKS, "w3v"),
                ("world", "w1", "w1v"),
                ("world", "w2", "w2v"),
            ]

            assert result.checkpoint["pending_sends"] == ["w3v"]

    @pytest.mark.parametrize(
        "channel_values",
        [
            {"channel1": "channel1v"},
            {},  # to catch regression reported in #10
        ],
    )
    def test_write_and_read_channel_values(
        self, channel_values: dict[str, Any]
    ) -> None:
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": "thread-4",
                    "checkpoint_id": "4",
                    "checkpoint_ns": "",
                }
            }
            chkpnt = empty_checkpoint()
            chkpnt["id"] = "4"
            chkpnt["channel_values"] = channel_values

            newversions: ChannelVersions = {
                "channel1": 1,
                "channel:with:colon": 1,  # to catch regression reported in #9
            }
            chkpnt["channel_versions"] = newversions

            saver.put(config, chkpnt, {}, newversions)

            result = next(saver.list({}))
            assert result.checkpoint["channel_values"] == channel_values

    def test_write_and_read_pending_writes(self) -> None:
        with PyMySQLSaver.from_conn_string(DEFAULT_URI) as saver:
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": "thread-5",
                    "checkpoint_id": "5",
                    "checkpoint_ns": "",
                }
            }
            chkpnt = empty_checkpoint()
            chkpnt["id"] = "5"
            task_id = "task1"
            writes = [
                ("channel1", "somevalue"),
                ("channel2", [1, 2, 3]),
                ("channel3", None),
            ]

            saver.put(config, chkpnt, {}, {})
            saver.put_writes(config, writes, task_id)

            result = next(saver.list({}))

            assert result.pending_writes == [
                (task_id, "channel1", "somevalue"),
                (task_id, "channel2", [1, 2, 3]),
                (task_id, "channel3", None),
            ]
