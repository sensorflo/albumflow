from albumflow.gphotos_api import MediaItem
from albumflow.gphotos_sync import make_plan


def test_new_album_uploads_everything():
    plan = make_plan({"b.jpg": "2", "a.jpg": "1"}, [], {})
    assert plan.upload == ["a.jpg", "b.jpg"]
    assert plan.keep == {} and plan.trash == []


def test_unchanged_is_kept():
    plan = make_plan({"a.jpg": "1"}, [MediaItem("m1", "a.jpg")], {"m1": "1"})
    assert plan.keep == {"a.jpg": "m1"}
    assert plan.upload == [] and plan.trash == []


def test_changed_is_replaced():
    plan = make_plan({"a.jpg": "new"}, [MediaItem("m1", "a.jpg")], {"m1": "old"})
    assert plan.upload == ["a.jpg"]
    assert plan.trash == ["m1"]


def test_removed_and_renamed():
    remote = [MediaItem("m1", "old-name.jpg"), MediaItem("m2", "gone.jpg")]
    plan = make_plan({"new-name.jpg": "1"}, remote, {"m1": "1", "m2": "2"})
    assert plan.upload == ["new-name.jpg"]
    assert sorted(plan.trash) == ["m1", "m2"]


def test_unknown_remote_item_is_replaced():
    # e.g. state file lost: we can't know which version it is, so re-upload
    plan = make_plan({"a.jpg": "1"}, [MediaItem("m1", "a.jpg")], {})
    assert plan.upload == ["a.jpg"] and plan.trash == ["m1"]


def test_duplicate_remote_keeps_one():
    remote = [MediaItem("m1", "a.jpg"), MediaItem("m2", "a.jpg")]
    plan = make_plan({"a.jpg": "1"}, remote, {"m1": "1", "m2": "1"})
    assert plan.keep == {"a.jpg": "m1"} and plan.trash == ["m2"] and plan.upload == []
