import shutil
import subprocess

import pytest

from albumflow import cli, completion


def test_table_has_commands_aliases_and_their_options():
    table = completion.table(cli.build_parser())
    top = [word for word, _ in table[""]]
    assert {"import", "i", "sync-clocks", "sc", "to-annex", "completion", "-h"} <= set(top)
    assert [w for w, _ in table["i"]] == [w for w, _ in table["import"]]  # an alias gets the same options
    assert "--readings-from" in [w for w, _ in table["sc"]]
    assert "powershell" in [w for w, _ in table["completion"]]  # choices of a positional argument


def test_quotes_are_escaped_for_powershell():
    assert completion._quote("don't") == "'don''t'"
    script = completion.powershell(cli.build_parser(), ["albumflow", "af"])
    assert "-CommandName 'albumflow', 'af'" in script
    assert "doesn''t exist yet" in script  # from the help of --create-album


@pytest.mark.skipif(not shutil.which("pwsh"), reason="PowerShell 7 not installed")
def test_completions_in_powershell(tmp_path):
    script = tmp_path / "completion.ps1"
    assert cli.main(["completion", "powershell", "-o", str(script)]) == 0
    probe = (f". '{script}'; "
             "foreach ($line in 'af s', 'af sc --re', 'af i party/x') {"
             " $r = TabExpansion2 -inputScript $line -cursorColumn $line.Length;"
             " ($r.CompletionMatches | ForEach-Object CompletionText) -join ' ' }")
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", probe], capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert out[0].split() == ["sync-clocks", "sc"]
    assert out[1].split() == ["--reading", "--readings-from"]
    assert out[2:] in ([], [""])  # album names are not completed
