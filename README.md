# Poly Snipper

A small Windows screenshot tool inspired by Snipaste, built for local personal use.

## Run

Double-click `run_poly_snipper_silent.vbs` for normal use. Use `run_poly_snipper.bat` when you want to see console errors.

You can also run:

```powershell
python poly_snipper.py
```

## Controls

- `Alt + A`: start region capture
- Drag: select a region
- `Esc`: cancel capture
- After capture: image is copied to clipboard, saved as PNG, and opened in a topmost editor window
- Editor tools: pen, rectangle, ellipse, arrow, text, color swatches, line width, undo
- `Copy`: copy the edited image
- `Save As`: save the edited image

Screenshots are saved to:

```text
%USERPROFILE%\Pictures\PolySnips
```

## Notes

This MVP uses Python, Tkinter, Pillow, and Win32 APIs via `ctypes`. It does not need a .NET SDK or extra package installs on this machine.

## Build EXE and Installer

```powershell
.\build_installer.ps1
```

The installer writes this current-user startup entry when the startup task is selected:

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\PolySnipper
```
