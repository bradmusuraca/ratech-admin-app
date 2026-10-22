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

; Sage 50's dialog uses custom ABD_Listbox controls (area list on the
; left, template list on the right) and BITMAPBUTTON buttons.
_Log("IE classes: " & StringReplace(WinGetClassList($hIE), @LF, " | "))

; Enumerate ABD_Listbox / ListBox instances with their x positions
Local $aLB[8][2]
Local $nLB = 0
For $iInst = 1 To 8
    Local $h = ControlGetHandle($hIE, "", "[CLASS:ABD_Listbox; INSTANCE:" & $iInst & "]")
    If $h = "" Then $h = ControlGetHandle($hIE, "", "[CLASS:ListBox; INSTANCE:" & $iInst & "]")
    If $h = "" Then ExitLoop
    Local $aPos = ControlGetPos($hIE, "", $h)
    $aLB[$nLB][0] = $h
    $aLB[$nLB][1] = 9999
    If IsArray($aPos) Then $aLB[$nLB][1] = $aPos[0]
    $nLB += 1
Next
_Log("listboxes found: " & $nLB)

Local $bSelected = False
Local $iMin = 0

; Leftmost listbox = program area; select "Inventory"
If $nLB >= 1 Then
    For $k = 1 To $nLB - 1
        If $aLB[$k][1] < $aLB[$iMin][1] Then $iMin = $k
    Next
    If _SelectLB($hIE, $aLB[$iMin][0], "Inventory") Then
        _Log("selected area 'Inventory'")
    Else
        _Log("WARN: could not select area 'Inventory'")
    EndIf
    Sleep(1000)
EndIf

; Other listbox(es) = templates; select the assemblies journal
For $k = 0 To $nLB - 1
    If $k = $iMin And $nLB > 1 Then ContinueLoop
    If _SelectLB($hIE, $aLB[$k][0], "Assembl") Then
        _Log("selected assembly template in listbox " & $k)
        $bSelected = True
        ExitLoop
    EndIf
Next

; Fallbacks for other Sage versions: ListView / TreeView
If Not $bSelected Then
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
EndIf
If Not $bSelected Then
    _Snap("no_template_control")
    _Log("WARN: no template control took the selection")
EndIf

; IMPORT button's accelerator is Alt+P (window text shows 'IMPORT&P')
WinActivate($hIE)
Sleep(300)
_Log("pressing Alt+P (IMPORT)")
_Log("IE text now: " & StringReplace(WinGetText($hIE), @CRLF, " | "))
Send("!p")
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

; Safety: bail if the wrong template opened rather than driving a
; different journal's import with our file.
Local $sTplTitle = WinGetTitle($hTpl)
If Not StringInStr($sTplTitle, "Assembl") And Not StringInStr($sTplTitle, "BOM") Then
    _Log("FAIL: wrong template opened (" & $sTplTitle & ") - aborting")
    WinClose($hTpl)
    Exit 5
EndIf

WinActivate($hTpl)
Sleep(400)

; Template window buttons are custom BITMAPBUTTONs; accelerators are
; visible in the window text as 'NAME&X'. Find Import's and press it.
Local $sTplText = WinGetText($hTpl)
_Log("template window text: " & StringReplace($sTplText, @CRLF, " | "))
_Log("template classes: " & StringReplace(WinGetClassList($hTpl), @LF, " | "))

; The editor's run button is &OK (Alt+O); Enter also works since the
; default button class is DefButton.
If StringInStr($sTplText, "&OK") Then
    _Log("pressing Alt+O (&OK runs the import)")
    Send("!o")
Else
    Local $aKey = StringRegExp($sTplText, "Import&([A-Za-z0-9])", 3)
    If IsArray($aKey) Then
        _Log("pressing Alt+" & $aKey[0] & " (Import accelerator)")
        Send("!" & $aKey[0])
    Else
        _Log("no OK/Import accelerator found - sending Enter")
        Send("{ENTER}")
    EndIf
EndIf
Sleep(1000)

; ---- handle pre-import prompts, then wait for completion ----
Local $bDone = False
Local $bBackedUp = False
For $i = 0 To 150   ; up to ~75 s
    Local $aT = WinList()
    For $j = 1 To $aT[0][0]
        If $aT[$j][0] = "" Then ContinueLoop
        Local $sTxt = WinGetText($aT[$j][1])
        ; "back up before importing - continue?" prompt -> Yes
        If Not $bBackedUp And StringInStr($sTxt, "back") And StringInStr($sTxt, "continue") Then
            _Log("backup warning: " & StringReplace($sTxt, @CRLF, " | "))
            WinActivate($aT[$j][1])
            Send("y")          ; Yes
            Sleep(500)
            Send("!y")         ; Alt+Y if underlined accelerator
            $bBackedUp = True
            Sleep(1000)
            ExitLoop
        EndIf
        ; completion / error dialog (title mentions Sage or text matches)
        If StringInStr($aT[$j][0], "Sage") Or StringInStr($sTxt, "import") Then
            If StringInStr($sTxt, "complete") Or StringInStr($sTxt, "finished") Or StringInStr($sTxt, "error") Then
                _Log("result dialog: " & StringReplace($sTxt, @CRLF, " | "))
                WinActivate($aT[$j][1])
                Send("{ENTER}")
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


Func _SelectLB($hWnd, $hCtl, $needle)
    ; ABD_Listbox ignores listbox messages, ControlSend keys AND letter
    ; type-ahead. Real clicks DO select rows, and the dialog text shows
    ; the selected area / template description - so click each visible
    ; row until the dialog text reports the target.
    WinActivate($hWnd)
    Sleep(300)
    Local $aPos = ControlGetPos($hWnd, "", $hCtl)
    If Not IsArray($aPos) Then Return False
    Local $rowH = 17
    Local $maxRows = Int($aPos[3] / $rowH)
    For $r = 0 To $maxRows - 1
        Local $y = Int($rowH * $r + $rowH / 2)
        ControlClick($hWnd, "", $hCtl, "left", 1, Int($aPos[2] / 2), $y)
        Sleep(350)
        If StringInStr(WinGetText($hWnd), $needle) Then
            _Log("  '" & $needle & "' selected by click at row " & $r)
            Return True
        EndIf
    Next
    _Log("  '" & $needle & "' not found in " & $maxRows & " visible rows")
    Return False
EndFunc

Func _Log($s)
    Local $h = FileOpen($sLog, $FO_APPEND)
    FileWriteLine($h, @YEAR & "-" & @MON & "-" & @MDAY & " " & @HOUR & ":" & @MIN & ":" & @SEC & "  " & $s)
    FileClose($h)
EndFunc

Func _Snap($tag)
    ; Dump the active window's title + text so a stuck dialog is diagnosable.
    _Log("snapshot [" & $tag & "] active=" & WinGetTitle("[ACTIVE]") & " text=" & StringReplace(WinGetText("[ACTIVE]"), @CRLF, " | "))
EndFunc
