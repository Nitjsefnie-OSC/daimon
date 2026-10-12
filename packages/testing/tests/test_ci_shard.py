from collections import Counter

import pytest
from daimon.testing.ci_shard import shard_for


@pytest.mark.parametrize("count", [3, 4])
def test_shards_partition_collection_with_duplicates_preserved(count: int) -> None:
    nodeids = [f"suite/test_example.py::test_case[{index}]" for index in range(130)]
    nodeids += [nodeids[17], nodeids[17]]

    partitions = [
        [nodeid for nodeid in nodeids if shard_for(nodeid, count) == shard]
        for shard in range(count)
    ]

    assert Counter(nodeid for partition in partitions for nodeid in partition) == Counter(nodeids)
    assert all(partitions)
    assert partitions == [
        [nodeid for nodeid in nodeids if shard_for(nodeid, count) == shard]
        for shard in range(count)
    ]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_shard_is_in_range(count: int) -> None:
    assert all(0 <= shard_for(f"test_{i}", count) < count for i in range(100))
