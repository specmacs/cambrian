from cambrian.feeds.cambrian_api import rows_from_response


def test_parses_live_columnar_shape():
    # The exact shape the live API returned for /evm/chains.
    payload = [{
        "columns": [{"name": "id", "type": "UInt32"}, {"name": "name", "type": "String"}],
        "data": [[8453, "base"]],
        "rows": 1,
    }]
    assert rows_from_response(payload) == [{"id": 8453, "name": "base"}]


def test_parses_bare_columnar_dict():
    payload = {"columns": [{"name": "a"}, {"name": "b"}], "data": [[1, 2], [3, 4]]}
    assert rows_from_response(payload) == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_passes_through_list_of_dicts():
    payload = [{"x": 1}, {"x": 2}]
    assert rows_from_response(payload) == payload


def test_data_envelope():
    payload = {"data": [{"x": 1}]}
    assert rows_from_response(payload) == [{"x": 1}]


def test_empty_and_junk():
    assert rows_from_response([]) == []
    assert rows_from_response({}) == []
    assert rows_from_response(None) == []
