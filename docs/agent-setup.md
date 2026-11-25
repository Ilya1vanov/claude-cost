# Set up claude-cost

Follow these steps in order. Run each shell command yourself. Say what you are doing in one line per step and keep the user's questions for steps 2, 3 and 5.

## 1. Install

Run the install script. It installs `uv` when missing and leaves the machine on the latest Release, so it is safe to re-run.

```
curl -fsSL https://raw.githubusercontent.com/Ilya1vanov/claude-cost/main/install.sh | sh
```

Done when the script prints `Installed: claude-cost <version>`. If it fails, show the user its error line and stop.

Note the command the script reports: `claude-cost`, or a full path when the script says the command is not on PATH yet. Use that command in place of `claude-cost` for every `claude-cost` command in steps 3 to 5, and tell the user the script's PATH hint.

## 2. Transcript retention

Claude Code deletes transcripts after `cleanupPeriodDays` days, 30 by default, and claude-cost can only report on transcripts that still exist. Keeping a year gives it a year of history to analyse.

Tell the user this, and that longer retention uses more disk space. Ask: "Set transcript retention to one year (365 days) in `~/.claude/settings.json`? Other settings stay as they are." On no, change nothing and go to step 3.

On yes, read `~/.claude/settings.json` and apply the rule to its `cleanupPeriodDays`:

- Missing, not a number (`false`, a string, `null`), or 1 to 364: set 365.
- 365 or more: keep it and tell the user.
- `0`: transcript saving is off, so claude-cost has nothing to read. Say so and ask separately: "Turn transcript saving on with 365 days?" Set 365 only on that yes.

If the file is not valid JSON, or not a JSON object, leave it untouched, tell the user why retention was not set, and go to step 3.

Merge, never overwrite: change only `cleanupPeriodDays`, keep every other key, and create the file with only that key when it is missing.

If you cannot write the file, show the user this command, which applies the same rule and leaves `0` alone. When the shell cannot find `uv` (a fresh install whose PATH is not updated), use the `uv` path from step 1's output, `~/.local/bin/uv` by default. When the command prints a Python traceback, the file is not a JSON object: tell the user and go on to step 3, leaving the file as it is. Claude Code users run it as `! <command>`; for any other agent, tell the user to paste it into Terminal.

```
uv run --no-project python -c 'import json,pathlib;p=pathlib.Path.home()/".claude"/"settings.json";d=json.loads(p.read_text()) if p.exists() else {};v=d.get("cleanupPeriodDays");d["cleanupPeriodDays"]=v if type(v)in(int,float)and(v==0 or v>=365)else 365;p.parent.mkdir(exist_ok=True);p.write_text(json.dumps(d,indent=2)+"\n")'
```

Done when the user declined, or the file was left untouched as above, or you read the file back and `cleanupPeriodDays` is the value the rule gives, or the user has the command.

## 3. Key pattern

The key pattern turns ticket keys found in sessions into Auto-spotlights.

First run `claude-cost config get spotlight_key_pattern`. When it prints a value other than `(none)`, show it and ask: "Keep this key pattern?" On yes, set nothing and go to step 4.

Otherwise ask: "Which ticket keys do you use, such as a Jira project key like PROJ? Give one or more, or a regular expression, or say skip."

- Keys (`PROJ`, `PROJ,OTHER`): build `(?:PROJ|OTHER)-\d+`.
- A regular expression: use it as given.
- Skip: set nothing. Tell the user Auto-spotlights stay off and that `claude-cost config set spotlight_key_pattern <regex>` turns them on later.

Set it, quoting the value:

```
claude-cost config set spotlight_key_pattern '<regex>'
```

Done when the command prints `spotlight_key_pattern = ...`, or the user skipped. On an error, show it to the user and ask again.

## 4. Integrity check

Run each check and print one line per check, `PASS <check>` or `FAIL <check>: <what failed>`.

1. Version: `claude-cost --version` prints the version of the latest Release. Read the latest from the `tag_name` field of `https://api.github.com/repos/Ilya1vanov/claude-cost/releases/latest` (drop the leading `v`).
2. Key pattern: `claude-cost config get spotlight_key_pattern` prints the value set in step 3. After a skip, it prints `(none)`.
3. Data: `claude-cost ingest` and then `claude-cost today` both exit 0.
4. Retention: read `cleanupPeriodDays` back from `~/.claude/settings.json`. Apply the step 2 rule: PASS when the file holds the value the rule gives. Print `PASS retention: <reason>, found <value>` (`missing` when absent) instead of a FAIL when step 2 left the value as it was: the user declined (`user declined`), the file was invalid or not an object and left untouched (`file left untouched`), or the value is `0` and the user said no to turning saving on (`saving left off`). Otherwise FAIL with the value found, or `missing`. Add a note that managed (org) settings may override it; they are not detected.

Done when every check has its line. For a FAIL, add the command's own error text.

## 5. Finish

Print the command `claude-cost serve`. It prints the dashboard address as `Serving <address>` on start; the port is the `port` setting.

Ask: "Start it now?" On yes, run `claude-cost serve` in the background and read its first line:

- `Serving <address>` or `Already serving <address>`: success. Report the address. Re-running is safe.
- An error that the port is in use (exit 1): show the user the error line, including its `claude-cost config set port N` hint.

On no, leave the printed command.
