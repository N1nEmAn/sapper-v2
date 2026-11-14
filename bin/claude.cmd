@echo off
rem Forwards to the Claude Code CLI installed under WorkBuddy's node.
rem Kept here so `claude` resolves from anywhere on PATH (D:\sapper-v2\bin is on it).
setlocal
set "CLAUDE_EXE=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
if exist "%CLAUDE_EXE%" (
  "%CLAUDE_EXE%" %*
  exit /b %errorlevel%
)
echo claude.exe not found at %CLAUDE_EXE% >&2
echo Reinstall with: npm i -g @anthropic-ai/claude-code --registry https://registry.npmmirror.com >&2
exit /b 1
