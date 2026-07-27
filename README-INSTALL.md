# Installing whispr

whispr is a Windows app that automatically records your Microsoft Teams
calls/meetings, transcribes them locally on your own machine (nothing is
sent to any cloud service), and saves a text transcript. It only works with
Microsoft Teams on Windows.

## Before you install: consent and recording policy

Recording calls may require the consent of everyone on the call, depending on
your state/country and organization's policy. That's on you to check before
you start using this - it's not something the software decides for you.

## Install (3 steps)

1. Download/clone this repository to your computer - anywhere is fine (your
   Desktop, Documents, etc.).
2. Double-click **`install.cmd`** in that folder.
3. Answer the two questions it asks (where to save your transcripts, and
   which microphone/speaker to use) - then wait. The first run downloads a
   one-time ~1GB setup package, so give it a few minutes on a normal home
   internet connection.

When it finishes, whispr is running and will start automatically every time
you log in to Windows - you don't need to do anything else. It sits quietly
in the background; look for its icon in the system tray (bottom-right, may
be in the hidden-icons overflow arrow).

If something goes wrong partway through, it's safe to just double-click
`install.cmd` again - re-running it won't break anything.

## What if my antivirus flags this?

whispr watches for Teams windows, opens your microphone/speakers directly,
and sets itself to start at login - all normal for this kind of tool, but
also the same pattern some antivirus/Defender heuristics flag on
unsigned software. If you get a warning, that's what's happening; there's
no code-signing certificate on this yet.

## Known limitations

This installer was built and tested on one specific machine and can't
account for every possible Windows setup. Known gaps, in case something
doesn't work as expected:

- **Classic Teams isn't supported** - only the current ("new") Teams client
  is. The installer checks for this and will warn you if it can't find the
  new client.
- **Outlook matters for meeting detection.** With no Outlook, new
  Outlook-for-Windows, or web-only Outlook, whispr can't tell a scheduled
  meeting from an ad-hoc call - it'll just ask you Yes/No every time instead
  of silently auto-recording meetings. Not dangerous, just more prompts.
- **Non-English Windows/Teams language** isn't supported - the call
  detection logic looks for specific English window titles.
- **Non-US date/time regional settings** may cause whispr to occasionally
  match the wrong calendar entry to a recording (affecting only the saved
  meeting title/attendees, not whether it records).
- **ARM64 laptops** (e.g. Surface Pro X) aren't supported - this is built
  for regular x64 Windows PCs.
- **No working microphone or no speaker-loopback capture device** on your
  machine means whispr can't capture that side of the call. The installer's
  final test will tell you plainly if either wasn't detected.
- A few internal fields in the saved transcript files (like `topic:
  [unsorted]`) are placeholders for a separate personal note-taking workflow
  of the original author's - harmless, just ignore them.

## Manual start / troubleshooting

- To start whispr right now instead of waiting for your next login,
  double-click **`run-whispr.cmd`** in the folder you installed to.
- `install.log` in that same folder records every step the installer took -
  useful if you need to report a problem.
- To change your microphone/speaker pick or output folder later, just run
  `install.cmd` again.
