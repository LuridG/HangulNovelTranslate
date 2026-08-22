# Agent Notes

## Project Entry Points

- GUI entry point: start.bat, which runs .venv\Scripts\python.exe main.py.
- CLI entry point: .venv\Scripts\python.exe main.py --input ...
- The GUI stores local UI state in .gui_config.json; this file is machine-local and must not be committed.

## Validation In Restricted Environments

The Codex execution sandbox may reject the repository virtual-environment interpreter with Windows status
0xC0000022 / STATUS_ACCESS_DENIED. This is an execution-policy problem, not evidence that Python syntax or
the GUI is broken. When this happens:

1. First compare the command with start.bat; do not assume the BAT uses a different interpreter.
2. Ask for an escalated command when a process must be launched outside the sandbox. Use a narrow prefix such as
   start.bat, cmd /c, or the project-local .venv\Scripts\python.exe command rather than a broad shell rule.
3. For GUI validation, run start.bat manually from a normal user desktop session. This is the most reliable check
   for Tk/customtkinter rendering, DPI behavior, tab layout, and interactive controls.
4. For a non-interactive smoke check, run start.bat --help or execute the CLI entry point with a small fixture.
5. If python.exe is blocked but the desktop BAT works, use the BAT/manual path for runtime verification and use
   git diff --check, source inspection, and fixture-level tests for the sandbox-side checks.
6. Record the exact exit code. -1073741790 is 0xC0000022; treat it as an access-denied launch failure.

## GUI Verification Checklist

- Open the app with start.bat.
- Confirm the fourth tab is 设置.
- Edit Base URL, Model, chunk size, or output directory in the left panel, switch to 设置, and verify the
  same values are present before saving.
- Edit the same values in 设置, save, return to the left panel, and verify the left controls update.
- Save and restart; verify app_config, llm_profiles, fonts, and output are restored from .gui_config.json.
- Change font size and row height; verify both the glossary and input-file trees update immediately.
- Switch an API preset and verify Base URL, API Key, and Model change in both places.

## Change Safety

- Preserve unrelated user changes in a dirty worktree.
- Use apply_patch for manual edits.
- Do not delete or reset files to recover from a failed validation attempt.
- Prefer narrow tests and static checks when the GUI cannot be launched in the current environment.
