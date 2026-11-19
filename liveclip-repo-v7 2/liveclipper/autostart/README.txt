Start the watcher automatically when you log in. Nothing here installs itself.

MAC
 1. Edit com.liveclipper.watch.plist: replace the placeholder /Users/YOURNAME/liveclipper (YOURNAME is your Mac username) with the full path of the folder holding watch.py.
 2. cp com.liveclipper.watch.plist ~/Library/LaunchAgents/
 3. launchctl load ~/Library/LaunchAgents/com.liveclipper.watch.plist
 Stop: launchctl unload ~/Library/LaunchAgents/com.liveclipper.watch.plist
 Not tested on a real Mac from our side. If it fails, run "python3 watch.py run" in Terminal instead.

WINDOWS (Task Scheduler, run in Command Prompt, change the folder path first)
 schtasks /Create /TN liveclipper-watch /SC ONLOGON /TR "cmd /c cd /d C:\\path\\to\\liveclipper && python watch.py run" /F
 Stop/remove: schtasks /Delete /TN liveclipper-watch /F
 Not tested on Windows from our side.

Either way your computer must be awake and online for the watcher to catch a stream.
