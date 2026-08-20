; CaveViewer per-user Windows installer.
; Build with scripts/windows/package.sh. The script supplies the preprocessor
; values below so paths are resolved by the build tooling, not by end users.

#ifndef AppVersion
  #error AppVersion must be supplied by scripts/windows/package.sh
#endif
#ifndef PayloadDir
  #error PayloadDir must be supplied by scripts/windows/package.sh
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by scripts/windows/package.sh
#endif
#ifndef OutputBaseName
  #error OutputBaseName must be supplied by scripts/windows/package.sh
#endif
#ifndef SetupIconFile
  #error SetupIconFile must be supplied by scripts/windows/package.sh
#endif

#define AppName "CaveViewer"
#define AppPublisher "CaveViewer"
#define AppPayloadDirectory "app-" + AppVersion

[Setup]
AppId={{9B39A7AF-4525-4B39-8C14-7B6BFC651E9B}
; A stable AppId makes subsequent installers share this installation's
; uninstaller log. Keep older versioned payloads so a successful update can
; retain its last known-good version until the user uninstalls CaveViewer.
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/CaveViewer/CaveViewer
AppSupportURL=https://github.com/CaveViewer/CaveViewer/issues
AppUpdatesURL=https://github.com/CaveViewer/CaveViewer/releases
DefaultDirName={localappdata}\Programs\CaveViewer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Leave PrivilegesRequiredOverridesAllowed unset: its default disallows
; overrides, preserving this installer’s per-user, non-elevated contract.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
SetupIconFile={#SetupIconFile}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppPayloadDirectory}\CaveViewer.exe
CloseApplications=yes
CloseApplicationsFilter=CaveViewer.exe
RestartApplications=no
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
#ifdef EnableCodeSigning
SignTool=CaveViewerSign
SignedUninstaller=yes
#endif

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}\{#AppPayloadDirectory}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\CaveViewer"; Filename: "{app}\{#AppPayloadDirectory}\CaveViewer.exe"; WorkingDir: "{app}\{#AppPayloadDirectory}"
Name: "{autodesktop}\CaveViewer"; Filename: "{app}\{#AppPayloadDirectory}\CaveViewer.exe"; WorkingDir: "{app}\{#AppPayloadDirectory}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#AppPayloadDirectory}\CaveViewer.exe"; Description: "Launch CaveViewer"; WorkingDir: "{app}\{#AppPayloadDirectory}"; Flags: nowait postinstall; Check: ShouldLaunchInstalledApplication

[Code]
const
  WAIT_OBJECT_0 = 0;
  WAIT_TIMEOUT = $00000102;
  SYNCHRONIZE = $00100000;
  MAX_PARENT_WAIT_MS = 300000;
  WINDOWS_INSTALLATION_REGISTRY_KEY = 'Software\CaveViewer\Installation';

function OpenProcess(
  DesiredAccess: Cardinal; InheritHandle: Boolean; ProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function HasArgument(const Expected: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do begin
    if CompareText(ParamStr(Index), Expected) = 0 then begin
      Result := True;
      Exit;
    end;
  end;
end;

function TryGetWaitPid(var WaitPid: Cardinal): Boolean;
var
  Index: Integer;
  Candidate: String;
  ParsedPid: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do begin
    Candidate := ParamStr(Index);
    if CompareText(Candidate, '--wait-pid') = 0 then begin
      if Index = ParamCount then begin
        Exit;
      end;
      Candidate := ParamStr(Index + 1);
    end else if Pos('--wait-pid=', Lowercase(Candidate)) = 1 then begin
      Delete(Candidate, 1, Length('--wait-pid='));
    end else begin
      Continue;
    end;

    ParsedPid := StrToIntDef(Candidate, 0);
    if ParsedPid <= 0 then begin
      Exit;
    end;
    WaitPid := ParsedPid;
    Result := True;
    Exit;
  end;
end;

function IsUpdateInstall(): Boolean;
begin
  Result := HasArgument('--update');
end;

function HasWaitPidArgument(): Boolean;
var
  Index: Integer;
  Candidate: String;
begin
  Result := False;
  for Index := 1 to ParamCount do begin
    Candidate := ParamStr(Index);
    if (CompareText(Candidate, '--wait-pid') = 0) or
       (Pos('--wait-pid=', Lowercase(Candidate)) = 1) then begin
      Result := True;
      Exit;
    end;
  end;
end;

function TryGetExpectedVersion(var ExpectedVersion: String): Boolean;
var
  Index: Integer;
  Candidate: String;
begin
  Result := False;
  for Index := 1 to ParamCount do begin
    Candidate := ParamStr(Index);
    if CompareText(Candidate, '--expected-version') = 0 then begin
      if Index = ParamCount then begin
        Exit;
      end;
      Candidate := ParamStr(Index + 1);
    end else if Pos('--expected-version=', Lowercase(Candidate)) = 1 then begin
      Delete(Candidate, 1, Length('--expected-version='));
    end else begin
      Continue;
    end;

    if Candidate = '' then begin
      Exit;
    end;
    ExpectedVersion := Candidate;
    Result := True;
    Exit;
  end;
end;

function HasExpectedVersionArgument(): Boolean;
var
  Index: Integer;
  Candidate: String;
begin
  Result := False;
  for Index := 1 to ParamCount do begin
    Candidate := ParamStr(Index);
    if (CompareText(Candidate, '--expected-version') = 0) or
       (Pos('--expected-version=', Lowercase(Candidate)) = 1) then begin
      Result := True;
      Exit;
    end;
  end;
end;

function IsVerificationOnly(): Boolean;
begin
  Result := HasArgument('--verify-install');
end;

function ValidateUpdateArguments(): Boolean;
var
  ExpectedVersion: String;
begin
  Result := False;
  if IsVerificationOnly() then begin
    SuppressibleMsgBox(
      '--verify-install cannot be combined with --update.', mbError, MB_OK, IDOK
    );
    Exit;
  end;
  if not TryGetExpectedVersion(ExpectedVersion) then begin
    SuppressibleMsgBox(
      '--update requires --expected-version <installer version>.', mbError, MB_OK, IDOK
    );
    Exit;
  end;
  if CompareText(ExpectedVersion, '{#AppVersion}') <> 0 then begin
    SuppressibleMsgBox(
      'The update installer version does not match the verified update package.',
      mbError,
      MB_OK,
      IDOK
    );
    Exit;
  end;
  Result := True;
end;

function WaitForParentProcess(): Boolean;
var
  WaitPid: Cardinal;
  ParentHandle: THandle;
  WaitResult: Cardinal;
begin
  Result := False;
  if not TryGetWaitPid(WaitPid) then begin
    SuppressibleMsgBox(
      '--update requires --wait-pid <positive process id>.', mbError, MB_OK, IDOK
    );
    Exit;
  end;

  ParentHandle := OpenProcess(SYNCHRONIZE, False, WaitPid);
  if ParentHandle = 0 then begin
    if DLLGetLastError = 87 then begin
      Result := True;
      Exit;
    end;
    SuppressibleMsgBox(
      'The update parent process could not be opened. Start CaveViewer again and retry the update.',
      mbError,
      MB_OK,
      IDOK
    );
    Exit;
  end;
  try
    WaitResult := WaitForSingleObject(ParentHandle, MAX_PARENT_WAIT_MS);
    if WaitResult = WAIT_OBJECT_0 then begin
      Result := True;
    end else if WaitResult = WAIT_TIMEOUT then begin
      SuppressibleMsgBox(
        'CaveViewer did not exit within five minutes. Close it and retry the update.',
        mbError,
        MB_OK,
        IDOK
      );
    end else begin
      SuppressibleMsgBox(
        'Unable to wait for CaveViewer before updating.', mbError, MB_OK, IDOK
      );
    end;
  finally
    CloseHandle(ParentHandle);
  end;
end;

function RunInstalledVerification(): Boolean;
var
  ResultCode: Integer;
  InstalledExecutable: String;
begin
  InstalledExecutable := ExpandConstant('{app}\{#AppPayloadDirectory}\CaveViewer.exe');
  Result := Exec(
    InstalledExecutable,
    '--update-branch',
    ExpandConstant('{app}\{#AppPayloadDirectory}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 2);
  if not Result then begin
    SuppressibleMsgBox(
      'The installed CaveViewer verification did not complete. The installer has not launched the new version.',
      mbError,
      MB_OK,
      IDOK
    );
  end;
end;

function RecordSuccessfulInstallation(): Boolean;
var
  PayloadDirectory: String;
begin
  PayloadDirectory := ExpandConstant('{app}\{#AppPayloadDirectory}');
  Result :=
    RegWriteStringValue(
      HKCU,
      WINDOWS_INSTALLATION_REGISTRY_KEY,
      'Channel',
      'windows_installer'
    ) and
    RegWriteStringValue(
      HKCU,
      WINDOWS_INSTALLATION_REGISTRY_KEY,
      'PayloadDirectory',
      PayloadDirectory
    ) and
    RegWriteStringValue(
      HKCU,
      WINDOWS_INSTALLATION_REGISTRY_KEY,
      'Version',
      '{#AppVersion}'
    );
  if not Result then begin
    SuppressibleMsgBox(
      'CaveViewer was installed but its update registration could not be saved.',
      mbError,
      MB_OK,
      IDOK
    );
  end;
end;

function LaunchInstalledApplication(): Boolean;
var
  ResultCode: Integer;
  InstalledExecutable: String;
  PayloadDirectory: String;
begin
  InstalledExecutable := ExpandConstant('{app}\{#AppPayloadDirectory}\CaveViewer.exe');
  PayloadDirectory := ExpandConstant('{app}\{#AppPayloadDirectory}');
  Result := Exec(
    InstalledExecutable,
    '',
    PayloadDirectory,
    SW_SHOWNORMAL,
    ewNoWait,
    ResultCode
  );
  if not Result then begin
    SuppressibleMsgBox(
      'CaveViewer was updated but the new application could not be launched.',
      mbError,
      MB_OK,
      IDOK
    );
  end;
end;

function InitializeSetup(): Boolean;
begin
  if IsUpdateInstall() then begin
    Result := ValidateUpdateArguments() and WaitForParentProcess();
  end else if HasWaitPidArgument() or HasExpectedVersionArgument() then begin
    SuppressibleMsgBox(
      '--wait-pid and --expected-version are only valid together with --update.',
      mbError,
      MB_OK,
      IDOK
    );
    Result := False;
  end else begin
    Result := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    if not RunInstalledVerification() then begin
      Abort;
    end;
    { Keep the last known-good marker until a complete new payload passes its
      controlled check. Old versioned payloads remain available for recovery. }
    if not RecordSuccessfulInstallation() then begin
      Abort;
    end;
    if IsUpdateInstall() and not LaunchInstalledApplication() then begin
      Abort;
    end;
  end;
end;

function ShouldLaunchInstalledApplication(): Boolean;
begin
  { An automatic update launches explicitly after verification so silent mode
    cannot skip or duplicate the relaunch through the postinstall checkbox. }
  Result := not IsVerificationOnly() and not IsUpdateInstall();
end;
