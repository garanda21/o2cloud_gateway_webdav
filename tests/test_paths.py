from o2gateway.cloud.base import basename, normalize_cloud_path, parent_path
from o2gateway.webdav.parsing import parse_range


def test_normalize_cloud_path():
    assert normalize_cloud_path("") == "/"
    assert normalize_cloud_path("/Fotos/../Docs/./a.txt") == "/Docs/a.txt"
    assert normalize_cloud_path("\\Fotos\\a.txt") == "/Fotos/a.txt"


def test_parent_and_basename():
    assert parent_path("/") == "/"
    assert parent_path("/Fotos/a.jpg") == "/Fotos"
    assert basename("/Fotos/a.jpg") == "a.jpg"


def test_parse_range():
    assert parse_range("bytes=0-99") == (0, 99)
    assert parse_range("bytes=100-") == (100, None)
    assert parse_range("bytes=-500") == (-500, None)
    assert parse_range("bogus") is None

