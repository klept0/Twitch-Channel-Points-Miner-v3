# Windows

[Back to the README](../README.md)

The Windows executable does not require Python, Git, or Command Prompt.

It includes every feature except the optional [Apprise](notifications.md#apprise) notifier.
All the other notification providers work. To use Apprise on Windows, run the
miner from a [source checkout](../README.md#source-checkout-quick-start) or Docker.

## Standard vs. portable

Two distributions are published on every release, identical in features and
differing only in where they keep configuration, Twitch login cookies,
analytics, and logs:

- **Standard** (`TwitchChannelPointsMiner-<version>-Setup.exe`, recommended):
  an installer that stores your data in your Windows user profile
  (`%LOCALAPPDATA%\TwitchChannelPointsMiner`), separate from the installed
  program. Reinstalling, updating, or uninstalling the app never touches it.
- **Portable** (`TwitchChannelPointsMiner-Portable-<version>.zip`): no
  installation - keeps everything beside the executable, so the whole folder
  can be moved, copied, or run from removable media. Keep its folder private,
  since it contains your Twitch login and saved session.

If you already used an older release (which always behaved like the portable
build, even when installed) and switch to the Standard installer, the app
detects your existing configuration, cookies, and analytics beside the old
executable on its first run, moves them into the new location automatically,
and shows a one-time confirmation dialog. Your original files are archived
untouched in a `config-legacy` folder next to where the old executable was,
so nothing is lost; this only happens once.

### Installers

Each release has three Standard installers. They install to the same place
and keep the same data, so running a different one later switches between
the desktop app and background mining.

| Installer | Installs |
| --- | --- |
| `TwitchChannelPointsMiner-<version>-Online-Setup.exe` | Asks whether you want the desktop app or background mining only, then downloads just that program. Needs an internet connection while it runs. |
| `TwitchChannelPointsMiner-<version>-Setup.exe` | The desktop app: a window with the dashboard and a tray icon. It can also mine in the background. |
| `TwitchChannelPointsMiner-<version>-Background-Setup.exe` | Background mining only: a smaller program with no window or tray icon. |

The online installer checks the SHA-256 of what it downloads against the
value built into it, and installs nothing if they differ or the download
fails. To install without it, use one of the two full installers. The
programs it downloads are on the release page as
`TwitchChannelPointsMiner-<version>-Desktop.exe` and
`TwitchChannelPointsMiner-<version>-Background.exe`.

## Install the Windows executable

1. Open the project's
   [Releases page](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/releases)
   and select the newest release.
2. Under **Assets**, download `TwitchChannelPointsMiner-<version>-Setup.exe`
   for the Standard installer (recommended), or
   `TwitchChannelPointsMiner-Portable-<version>.zip` for the portable build.
   Do not download **Source code**.
   - **Installer:** run the downloaded `.exe` and follow its prompts.
   - **Portable:** open your **Downloads** folder, right-click the ZIP file,
     select **Extract All...**, choose a permanent location you can easily
     find (such as `Documents\TwitchChannelPointsMiner`), and select
     **Extract**. Do not run the program from inside the ZIP file.
3. Launch the app (the installer offers to do this automatically; for the
   portable build, open the extracted folder and double-click
   `TwitchChannelPointsMiner-Portable.exe`). The first run creates the
   configuration file and opens a desktop window directly on the **Config**
   tab, ready for you to fill in.

Only download the executable from the project's official Releases page. If
Microsoft Defender SmartScreen appears, check that the publisher warning names
the file you downloaded and that you used the link above. Then select
**More info** followed by **Run anyway**. If the file came from anywhere else,
delete it instead.

## Set up your account

The desktop window has two tabs: **Dashboard** (the analytics dashboard,
embedded) and **Console** (the miner's live log output). On first run it
opens straight to the Dashboard tab's **Config** view.

1. If the window asks for a username and password, use your Twitch username
   and the password printed on the **Console** tab (also saved in
   `config\config.py`, under `ANALYTICS_CONFIG`).
2. Under **Configured streamers**, add the channels you want to watch.
3. Review the other Config tab sections - categories, sources, logging, and
   notifications. The [configuration guide](../README.md#configuration-file) explains
   every section in more detail; anything not covered by the Config tab can
   still be edited directly in `config\config.py` with Notepad.

Changes made in the Config tab are written to `config\config.py`
automatically; streamer and category changes take effect immediately, and
other settings after a restart. You can return to this tab any time to enable
followed channels, Drops categories, predictions, notifications, and other
settings.

## Start the miner

The miner starts automatically as soon as the desktop window opens - there is
no separate step. Watch its progress on the **Dashboard** tab. If Twitch sign-in
is needed, a window pops up with the activation link and code to enter (also
available, along with startup errors if the dashboard itself fails to load, on
the **Console** tab). Use **Open in browser** on either tab to view the
dashboard in your default browser instead.

Closing the window stops the miner. Where its settings, login session, and
logs are saved depends on which distribution you installed (see
[Standard vs. portable](#standard-vs-portable) above):

- **Standard:** in your Windows user profile
  (`%LOCALAPPDATA%\TwitchChannelPointsMiner`), independent of the installed
  program's location.
- **Portable:** in folders beside the executable. Do not move the executable
  by itself after setup; move the entire `TwitchChannelPointsMiner` folder if
  you want it in a different location.

## Run in the background

Background mining runs the miner with no window or tray icon. Set it up in
either of these ways:

- Install background mining only, with the Background or Online installer
  (see [Installers](#installers)).
- In the desktop app's installer, select **Run in the background when you
  sign in** on the **Select Additional Tasks** page.

Both work the same way:

- **What it does:** the miner starts with no window. If it crashes, it
  restarts after 30 seconds, waiting longer (up to 10 minutes) if it keeps
  crashing. It runs as your Windows user, with your normal settings, login
  session, and logs.
- **When it starts:** by default, when you sign in to Windows. The installer
  creates a scheduled task named **Twitch Channel Points Miner
  (background)** for your account; if Windows doesn't allow that, it adds a
  startup entry instead. Background mining replaces **Start automatically
  when you sign in**.
- **Start at boot (optional):** select **Start when Windows starts, before
  anyone signs in** (the desktop installer calls it **Also start when Windows
  starts**). See [Start at boot](#start-at-boot).
- **Sign in once first:** background mining can't show Twitch's activation
  code. With the desktop app, open it once, enter your Twitch username and
  sign in, then close it. With background mining only, the installer opens a
  setup window that asks for your username and shows the activation code;
  run **Set up background mining** from the Start menu to do it again. Until
  then, background mining exits without mining and says why in
  `logs\background.log`.
- **Start, stop, and check it** from the Start menu:
  - **Start background mining** starts it now, through the scheduled task
    when there is one. If you haven't signed in to Twitch yet, it opens the
    sign-in instead.
  - **Stop background mining** finishes the session cleanly, like closing the
    window does.
  - **Background mining status** says whether it's running and when it
    starts, and shows the last line of its log.
  - **Background mining logs** opens the `logs` folder of
    `%LOCALAPPDATA%\TwitchChannelPointsMiner`:
    - `background.log`: starts, stops, crashes, and restarts.
    - `background-worker.log`: the miner's console output.
    - The miner's own log files, as described in [Logs](logs.md).

  The [analytics dashboard](analytics.md) also works in your browser if it's
  enabled.
- **One miner at a time:** while background mining runs, the desktop app
  won't open, and the other way round. Stop background mining first.
- **Updates and repairs:** the installer stops background mining before it
  replaces the program and sets it up again afterwards.
- **Turn it off:** run the desktop installer again and clear the option, or
  install the desktop app over background mining only. Uninstalling also
  stops background mining and removes the task. None of these touch your
  settings, Twitch login, analytics, or logs.

### Start at boot

With this option, the scheduled task also runs when Windows starts, whether
or not anyone signs in, so the miner keeps mining while you're signed out.

- **Administrator permission:** creating a task that runs before sign-in
  needs one administrator (UAC) prompt while installing. If you decline it,
  or it fails, background mining starts when you sign in instead, and
  `logs\background.log` says why.
- **Account:** the task still runs as your own account, and Windows doesn't
  store your password for it. Because of that, it can reach the internet but
  not network shares, so keep the miner's data in your user profile (the
  default).
- **Account rights:** Windows runs such a task with the **Log on as a batch
  job** right. Administrator accounts have it. If the task doesn't start on a
  standard account, run **Background mining status** to check, and grant the
  right in Local Security Policy, or turn the option off.
- **After you sign in:** the miner started at boot keeps running, and signing
  out doesn't stop it. Use **Stop background mining** to stop it.

To supervise a source checkout instead, see [supervisor](supervisor.md).

## Update the miner

- **Standard:** download and run the newest installer from the
  [Releases page](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/releases)
  and follow its prompts; it updates the installed program in place. The
  online installer offers what you chose last time. Your
  configuration and login data live outside the install directory and are
  never affected.
- **Portable:**
  1. Stop the running miner.
  2. Download and extract the newest portable ZIP as described above.
  3. Copy the new `TwitchChannelPointsMiner-Portable.exe` into your existing
     miner folder.
  4. When Windows asks, choose **Replace the file in the destination**.
  5. Start the miner normally. Your existing configuration and login data
     remain in place.

## Windows troubleshooting

- **The desktop window never appears, or a console window flashes and
  closes:** The executable needs the Microsoft Edge WebView2 runtime, which
  ships with Windows 11 and most up-to-date Windows 10 installs. If it's
  missing, install the
  [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  and run the executable again.
- **The Dashboard tab shows "The dashboard isn't available yet":** Check the
  Console tab for the reason. Analytics may still be starting up, or
  `enable_analytics` may be set to `False` in `config\config.py`.
- **Windows says it cannot find the configuration (portable build only):**
  Right-click the downloaded ZIP and use **Extract All...** before running the
  executable.
- **"Notifications to apprise are failing: The Windows executable does not
  include Apprise":** The executable cannot send through Apprise. Remove
  `apprise=Apprise(...)` from `MINER_CONFIG["logger_settings"]` and use a
  built-in provider such as Discord, Telegram or ntfy, or see
  [Apprise](notifications.md#apprise) for the setups that include it.
- **Emoji or symbols look broken:** In `config.py`, use
  `LoggerSettings(emoji=False)` in `MINER_CONFIG["logger_settings"]`. See
  [LoggerSettings](settings.md#loggersettings) for the complete example.
- **The miner closes or reports an error after setup:** Check the Console tab,
  or use the desktop window's **Open logs folder** button (or open
  `%LOCALAPPDATA%\TwitchChannelPointsMiner\logs` for a Standard install, or
  the `logs` folder beside the executable for a portable one) and check the
  newest log file. Remove account names, cookies, tokens, and passwords
  before sharing a log in a bug report.

Developers who want to create the executable from source can use the
[Windows build guide](../BUILD.md#windows-executable).
