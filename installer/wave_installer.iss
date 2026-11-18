; wave_installer.iss - Inno Setup script for Wave AI (Tier 22).
;
; Wraps the already-built dist\WaveAI.exe (wave.spec / build_exe.bat, see
; docs/PACKAGING.md) in a real Windows installer: Start Menu shortcut,
; optional desktop icon, uninstaller, and a "launch on finish" step.
;
; Requires Inno Setup 6 (https://jrsoftware.org/isinfo.php) and a Windows
; machine - neither is available in the sandbox this was written in, so
; this script has been written carefully against Inno's documented syntax
; but has NEVER been compiled or run. Compiling and clicking through it on
; a real Windows machine is this tier's own "needs your PC" item (see
; docs/MANUAL_TEST_CHECKLIST.md, "Tier 22").
;
; Build order:
;   1. Build the .exe first (docs/PACKAGING.md):
;        .\wave_env\Scripts\Activate.ps1
;        pip install -r requirements.txt pyinstaller
;        .\build_exe.bat
;      This produces dist\WaveAI.exe.
;   2. Compile this installer (from the project root, with Inno Setup's
;      compiler on PATH):
;        iscc installer\wave_installer.iss
;      Produces installer\output\WaveAI_Setup.exe.
;
; Deliberately NOT a "first-run model downloader" of its own: Wave's own
; setup wizard (ui/onboarding.py, Tier 21) already has a consent-gated
; Local Voice step that downloads a Piper voice on first run
; (config.TTS_LOCAL_AUTO_DOWNLOAD, with a pinned-sha256 checksum - see
; voice/tts.py's HAS_PIPER block). Duplicating that logic in Pascal Script
; here would mean two consent flows that can drift out of sync with each
; other. The installer's [Run] step below just launches WaveAI.exe once -
; the app's own wizard does the rest, the same way it would for anyone who
; ran the bare .exe without this installer at all. The acoustic "Hey Wave"
; wake-word model (voice_models/wakewords/hey_wave.onnx) needs no download
; step either way - wave.spec already bundles it straight into the .exe.

#define AppName "Wave AI"
#define AppVersion "1.0.0"
#define AppPublisher "Wave AI contributors"
#define AppExeName "WaveAI.exe"

[Setup]
AppId={{B7E1F8D2-5C3A-4E6B-9F12-7A3D8C9B1E4A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Installs per-user, under %LOCALAPPDATA% - no admin rights needed, and no
; UAC prompt, matching how config.py already resolves a writable DATA_DIR
; next to a frozen .exe. "dialog" lets someone who DOES want an all-users
; install (Program Files) choose that instead at install time.
DefaultDirName={localappdata}\Programs\WaveAI
DefaultGroupName=Wave AI
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=output
OutputBaseFilename=WaveAI_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; No code signing certificate (see docs/PACKAGING.md's own "Before you hand
; the .exe to anyone" section) - SmartScreen/AV warnings on first run are
; expected for an unsigned installer, not a sign this script is broken.
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#AppExeName}
; Allow ESC-free, non-destructive re-installs/upgrades over an existing one.
DisableWelcomePage=no
DisableReadyPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "launchatlogin"; Description: "Start Wave AI automatically when I sign in"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The .exe itself. "dist\WaveAI.exe" must already exist - see the build
; order comment above. Flags: ignoreversion since WaveAI.exe carries no
; embedded version resource yet (wave.spec doesn't set one) - Inno can't
; compare versions it can't read, so it always overwrites on reinstall,
; which is the right behavior here anyway (a "repair" should just replace
; the one file).
Source: "..\dist\WaveAI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Readme.MD"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
; If you've built a custom app icon (assets\icon.ico - wave.spec already
; picks it up automatically if present), ship it alongside the .exe so the
; Start Menu/desktop shortcuts below can reference it without relying on
; the icon PyInstaller embedded into the .exe itself:
; Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Wave AI"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall Wave AI"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Wave AI"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\Wave AI"; Filename: "{app}\{#AppExeName}"; Tasks: launchatlogin

[Run]
; Launch once, unchecked by default is wrong here - most installers DO
; offer to launch on finish, and Wave's first run is also its setup
; wizard, so offering it is the natural "get started" moment.
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove nothing under the user's own data folder (config.DATA_DIR, which
; for a frozen build lives next to the .exe or in %LOCALAPPDATA%\WaveAI -
; see settings/config.py's resolve_dirs()) by default: chat history, saved
; memories, reminders and notes are the user's own data, not installed
; files, and silently deleting them on a plain "uninstall the program"
; click would be a surprising, unrecoverable action for exactly the kind
; of data this project's own Readme "Privacy & Data Flow" section promises
; stays local and under the user's control. A user who wants a true clean
; wipe already has "Settings > Privacy > Clear All Local Data" inside the
; app itself (automation/security_log.py's clear(), database/db_manager.py's
; equivalents) before they uninstall - a deliberate, in-app, reversible-
; until-confirmed action, not an installer uninstall side effect.
