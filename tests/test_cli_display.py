from albumflow.cli import ProgressDisplay


def test_bars_stay_and_backup_is_last(capsys):
    display = ProgressDisplay()
    backup = display.bar(lambda: "backup F: SSD", last=True)
    card1 = display.bar("import E: CARD1")
    backup(1, 10, "x")
    card1(5, 5, "x")  # complete
    card2 = display.bar("import E: CARD2")
    card2(5, 5, "x")
    display.log("done")
    last_frame = capsys.readouterr().err.split("done\n")[-1]
    labels = [line.split("  [")[0].strip() for line in last_frame.splitlines() if "[" in line]
    assert labels == ["import E: CARD1", "import E: CARD2", "backup F: SSD"]
