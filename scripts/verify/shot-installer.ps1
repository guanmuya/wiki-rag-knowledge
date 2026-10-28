# Capture the NSIS installer window to verify its UI language.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .tmp-verify/shot-installer.ps1 <installer.exe> <out.png>
# Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI and CJK text breaks parsing.
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$Out
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Rect {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
}
"@

$proc = Start-Process -FilePath $Installer -PassThru
$handle = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 250
  $proc.Refresh()
  if ($proc.HasExited) { break }
  if ($proc.MainWindowHandle -ne [IntPtr]::Zero) { $handle = $proc.MainWindowHandle; break }
}
if ($handle -eq [IntPtr]::Zero) {
  Write-Output "FAIL: no installer window handle (process exited?)"
  if (-not $proc.HasExited) { $proc.Kill() }
  exit 1
}

# Bring to front, then capture with PrintWindow (works even if occluded).
[void][Win32Rect]::SetWindowPos($handle, [IntPtr]::new(-1), 0, 0, 0, 0, 0x0001 -bor 0x0002)
[void][Win32Rect]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 1200

$rect = New-Object Win32Rect+RECT
[void][Win32Rect]::GetWindowRect($handle, [ref]$rect)
$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
Write-Output ("window: {0}x{1} at ({2},{3})" -f $w, $h, $rect.Left, $rect.Top)

$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [Win32Rect]::PrintWindow($handle, $hdc, 2)
$g.ReleaseHdc($hdc)
if (-not $ok) {
  Write-Output "WARN: PrintWindow returned false, falling back to screen copy"
  $g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bmp.Size)
}
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved: $Out"

if (-not $proc.HasExited) {
  $proc.CloseMainWindow() | Out-Null
  Start-Sleep -Milliseconds 800
  if (-not $proc.HasExited) { $proc.Kill() }
}
Write-Output "DONE"
