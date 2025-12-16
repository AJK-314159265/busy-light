# Running the Python App at Windows 11 Startup

This guide explains how to configure Windows 11 to automatically run a Python application at startup using the **Startup** folder and a **shortcut**.

In this example, we will start:

- Python interpreter: `C:\Python313\pythonw.exe`  
- Script: `C:\busy light\calendar_busy_light.py`  

You can adapt the paths to match your own installation.

## 1. Open the Startup Folder

1. Press **`Win + R`** on your keyboard to open the **Run** dialog.
2. Type:

   ```text
   shell:startup
   ```

3. Press **Enter**.  
   This opens the **Startup** folder for your user account. Any program or shortcut placed here will run automatically when you log in.

## 2. Create a Shortcut to Your Python Script

1. In the **Startup** folder, right-click on an empty area.
2. Select **New → Shortcut**.
3. In the **Type the location of the item** field, enter:

   ```text
   C:\Python313\pythonw.exe "C:\busy light\calendar_busy_light.py"
   ```

   - `pythonw.exe` runs Python without opening a console window.
   - The script path is enclosed in quotes because the folder name contains a space (`busy light`).

4. Click **Next**.
5. Enter a descriptive name for the shortcut, for example:

   ```text
   Busy Light – Calendar Script
   ```

6. Click **Finish**.

The shortcut will now appear in the **Startup** folder.

## 3. Test the Configuration

To confirm that everything works:

1. **Restart** your computer or **sign out** and sign back in.
2. After logging in, Windows should automatically run the Python script in the background via `pythonw.exe`.

If the script does not start:

- Double-check the paths in the shortcut target.
- Ensure that `pythonw.exe` exists at `C:\Python313\`.
- Verify that `calendar_busy_light.py` runs correctly when started manually.
