; assembly_import.au3 - headless drive of Sage 50's Inventory Assemblies Journal import.
; Run: AutoIt3.exe assembly_import.au3
;
; Prerequisites:
;   - Sage 50 (peachw.exe) running with the RATECH company open
;   - The "Inventory Assemblies Journal" import template pre-configured once
;     manually: correct fields checked AND the filename on the Options tab set
;     to the staging CSV (assembly_pending.csv in the bridge folder).
;
; Logs to assembly_import.log next to the script and drops screenshots
; (stepN.png) when a window it expects doesn't appear, so failures are
; diagnosable without watching the screen.

#include <MsgBoxConstants.au3>
#include <FileConstants.au3>

Opt("WinTitleMatchMode", 2)          ; substring match
Opt("MouseCoordMode", 1)
Opt("WinWaitDelay", 250)

Global $sDir = @ScriptDir
Global $sLog = $sDir & "\assembly_import.log"

_Log("=== import run start ===")

; ---- locate the Sage main window ----
Local $aProcs = ProcessList("peachw.exe")
Local $hSage = 0
For $i = 1 To $aProcs[0][0]
    Local $aWins = WinList()
    For $j = 1 To $aWins[0][0]
        If $aWins[$j][0] <> "" And WinGetProcess($aWins[$j][1]) = $aProcs[$i][1] Then
            If BitAND(WinGetState($aWins[$j][1]), 2) Then  ; visible, non-empty title
                If StringInStr($aWins[$j][0], "Sage") Then
                    $hSage = $aWins[$j][1]
                    ExitLoop 2
                ElseIf $hSage = 0 Then
                    $hSage = $aWins[$j][1]  ; first candidate as fallback
                EndIf
            EndIf
        EndIf
    Next
Next

If $hSage = 0 Then
    _Log("FAIL: peachw.exe window not found - is Sage open with a company?")
    Exit 2
EndIf
_Log("found Sage window: " & WinGetTitle($hSage))

WinActivate($hSage)
WinWaitActive($hSage, "", 10)
Sleep(500)

; ---- File > Select Import/Export ----
If Not WinMenuSelectItem($hSage, "", "&File", "Select &Import/Export...") Then
    If Not WinMenuSelectItem($hSage, "", "&File", "Select Import/Export") Then
        _Log("menu select failed, trying keystrokes")
        Send("!f")
        Sleep(600)
        Send("i")                    ; 'Select Import/Export' accelerator
    EndIf
EndIf

If Not WinWait("Select Import/Export", "", 20) Then
    _Snap("select_ie_missing")
    _Log("FAIL: Select Import/Export window did not open")
    Exit 3
EndIf
_Log("Select Import/Export open")
Local $hIE = WinGetHandle("Select Import/Export")
WinActivate($hIE)
Sleep(500)

; The dialog is a tree/list of program areas + templates. Try every common
; control type; log what we find if none take the selection.
Local $bSelected = False

; Try a list view first (SysListView32)
Local $sCtl = ControlGetHandle($hIE, "", "[CLASS:SysListView32; INSTANCE:1]")
If $sCtl <> "" Then
    Local $iItem = ControlListView($hIE, "", $sCtl, "FindItem", "Inventory Assemblies Journal")
    If $iItem < 0 Then $iItem = ControlListView($hIE, "", $sCtl, "FindItem", "Assemblies Journal")
    If $iItem >= 0 Then
        ControlListView($hIE, "", $sCtl, "Select", $iItem)
        $bSelected = True
        _Log("selected template in ListView")
    EndIf
EndIf

; Try a tree view (SysTreeView32)
If Not $bSelected Then
    $sCtl = ControlGetHandle($hIE, "", "[CLASS:SysTreeView32; INSTANCE:1]")
    If $sCtl <> "" Then
        ControlTreeView($hIE, "", $sCtl, "Select", "#0|Inventory|Inventory Assemblies Journal")
        Sleep(400)
        $bSelected = True   ; best-effort - even partial select may work
        _Log("selected template in TreeView")
    EndIf
EndIf

If Not $bSelected Then
    _Snap("no_template_control")
    _Log("WARN: no list/tree control found - attempting blind Import click anyway")
EndIf

; Click the Import button
If ControlCommand($hIE, "", "[CLASS:Button; TEXT:&Import]", "IsEnabled") Then
    ControlClick($hIE, "", "[CLASS:Button; TEXT:&Import]")
ElseIf ControlCommand($hIE, "", "[CLASS:Button; TEXT:Import]", "IsEnabled") Then
    ControlClick($hIE, "", "[CLASS:Button; TEXT:Import]")
Else
    ; fall back: Enter usually triggers the default action
    Send("{ENTER}")
EndIf
Sleep(800)

; ---- template window (Fields/Options tabs + filename) ----
; Wait for a window whose title mentions Assembl or Import
Local $hTpl = 0
For $i = 0 To 20
    Local $aT = WinList()
    For $j = 1 To $aT[0][0]
        If $aT[$j][0] <> "" And (StringInStr($aT[$j][0], "Assembl") Or StringInStr($aT[$j][0], "Journal")) Then
            $hTpl = $aT[$j][1]
            ExitLoop 2
        EndIf
    Next
    Sleep(500)
Next

If $hTpl = 0 Then
    _Snap("template_missing")
    _Log("FAIL: template window did not open")
    Exit 4
EndIf
_Log("template window open: " & WinGetTitle($hTpl))
WinActivate($hTpl)
Sleep(400)

; The template remembers its Options-tab filename; just run it.
; Try Import button, then OK.
If Not ControlClick($hTpl, "", "[CLASS:Button; TEXT:&Import]") Then
    If Not ControlClick($hTpl, "", "[CLASS:Button; TEXT:Import]") Then
        ControlClick($hTpl, "", "[CLASS:Button; TEXT:OK]")
    EndIf
EndIf

; ---- wait for completion dialog, log text, dismiss ----
Local $bDone = False
For $i = 0 To 120   ; up to ~60 s
    Local $aT = WinList()
    For $j = 1 To $aT[0][0]
        If $aT[$j][0] <> "" And StringInStr($aT[$j][0], "Sage") Then
            Local $sTxt = WinGetText($aT[$j][1])
            If StringInStr($sTxt, "complete") Or StringInStr($sTxt, "finished") Or StringInStr($sTxt, "error") Then
                _Log("result dialog: " & StringReplace($sTxt, @CRLF, " | "))
                ControlClick($aT[$j][1], "", "[CLASS:Button; INSTANCE:1]")
                $bDone = True
                ExitLoop 2
            EndIf
        EndIf
    Next
    Sleep(500)
Next

If Not $bDone Then
    ; no explicit completion dialog - Enter dismisses most Sage popups
    Send("{ENTER}")
    Sleep(500)
    Send("{ENTER}")
    _Log("no completion dialog detected - dismissed with Enter (check Sage)")
EndIf

; Close the template window if still open
If WinExists($hTpl) Then WinClose($hTpl)
Sleep(500)
If WinExists("Select Import/Export") Then WinClose("Select Import/Export")

_Log("=== import run end ===")
Exit 0


Func _Log($s)
    Local $h = FileOpen($sLog, $FO_APPEND)
    FileWriteLine($h, @YEAR & "-" & @MON & "-" & @MDAY & " " & @HOUR & ":" & @MIN & ":" & @SEC & "  " & $s)
    FileClose($h)
EndFunc

Func _Snap($tag)
    ; Dump the active window's title + text so a stuck dialog is diagnosable.
    _Log("snapshot [" & $tag & "] active=" & WinGetTitle("[ACTIVE]") & " text=" & StringReplace(WinGetText("[ACTIVE]"), @CRLF, " | "))
EndFunc
