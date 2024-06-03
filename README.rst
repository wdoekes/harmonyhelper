HarmonyHelper
=============

*Convert MIDI files to CSV, MIDI or MP3.*

Primary transformations:

- Read MIDI file, convert to MP3 (or CSV or MIDI).

Optional additional transformations:

- Remove left/right panning (useful for the next options);

- select track to highlight (increased volume);

- pan the highlighted track to the right ear;

- invert highlighting (make the highlighted track *less* prominent);

- convert chords into single notes by choosing either the lower or
  higher part of the divisi;

- add a metronome track (hihat);

- swap out the instrument for a more clear one.

This tool doesn't do anything that cannot be done with a good MIDI
player, but it is *very* convenient.

**This tool has proven useful for people practicing in a choir. They can
highlight their own track (Soprano, Alt, ...) whilst also hearing the
context from the other parties. The conversion to MP3 makes practicing
in the car or anywhere else easy.**


-----
Usage
-----

The *HarmonyHelper* has a command line and a web-interface mode.

Command line interface:

.. code-block:: console

    $ python3 harmonyhelper.py input.mid output.mp3
    Reading file input.mid

    Do you wish to remove panning?
    1. yes
    2. no
    Your choice?
    ...

Web interface::

    Choir MID alterations: begin

    Upload your midi file here:

    +-------------+  +--------+
    | Choose file |  | Submit |
    +-------------+  +--------+

::

    Choir MID alterations: options
    input.mid

    Do you wish to remove original panning?

    [ ] no
    [ ] yes

    ...

    What format should the output file have?

    [ ] MIDI (.mid), the default
    [ ] MP3 (.mp3), an audio file suitable for playing on various
        devices (takes up to a minute! be patient!)
    [ ] CSV (.csv), a comma separated text file

    +--------+
    | Submit |
    +--------+

Test interface:

.. code-block:: console

    $ RUNTESTS=1 python3 harmonyhelper.py
    ...
    Ran n test(s) in 0.123s


-------------
Configuration
-------------

Dependencies:

- *Python* stdlib;

- *midicsv* (providing ``midicsv`` and ``csvmidi``) for conversion
  between MID and CSV;

- *TiMidity++* (providing ``timidity``) for conversion to audio;

- *SoX* (providing ``sox``) for simple audio post-processing and
  conversion to MP3.

Enabling the web interface in *Apache2* should be as simple as::

    <Directory "/path/to/harmonyhelper">
        <FilesMatch "^harmonyhelper[.]py$">
            Options +ExecCGI
            SetHandler cgi-script
        </FilesMatch>
    </Directory>
