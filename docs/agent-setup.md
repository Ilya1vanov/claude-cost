# Set up claude-cost

Follow these steps in order. Run each shell command yourself. Say what you are doing in one line per step and keep the user's questions for steps 2 and 4.

## 1. Install

Run the install script. It installs `uv` when missing and leaves the machine on the latest Release, so it is safe to re-run.

```
curl -fsSL https://raw.githubusercontent.com/Ilya1vanov/claude-cost/main/install.sh | sh
```

Done when the script prints `Installed: claude-cost <version>`. If it fails, show the user its error line and stop.

## 2. Key pattern

The key pattern turns ticket keys found in sessions into Auto-spotlights.

Ask: "Which ticket keys do you use, such as a Jira project key like PROJ? Give one or more, or a regular expression, or say skip."

- Keys (`PROJ`, `PROJ,OTHER`): build `(?:PROJ|OTHER)-\d+`.
- A regular expression: use it as given.
- Skip: set nothing. Tell the user Auto-spotlights stay off and that `claude-cost config set spotlight_key_pattern <regex>` turns them on later.

Set it, quoting the value:

```
claude-cost config set spotlight_key_pattern '<regex>'
```

Done when the command prints `spotlight_key_pattern = ...`, or the user skipped. On an error, show it to the user and ask again.

## 3. Integrity check

Run each check and print one line per check, `PASS <check>` or `FAIL <check>: <what failed>`.

1. Version: `claude-cost --version` prints the version of the latest Release. Read the latest from the `tag_name` field of `https://api.github.com/repos/Ilya1vanov/claude-cost/releases/latest` (drop the leading `v`).
2. Key pattern: `claude-cost config get spotlight_key_pattern` prints the value set in step 2. After a skip, it prints `(none)`.
3. Data: `claude-cost ingest` and then `claude-cost today` both exit 0.

Done when every check has its line. For a FAIL, add the command's own error text.

## 4. Finish

Print `claude-cost serve` and the dashboard address `http://127.0.0.1:8765/`.

Ask: "Start it now?" On yes, run `claude-cost serve` in the background and report the address. On no, leave the printed command.
