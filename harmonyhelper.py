#!/usr/bin/env python3
import cgi
import cgitb
import codecs
import os
import sys

from base64 import decodebytes, encodebytes
from collections import OrderedDict, defaultdict, namedtuple
from io import BytesIO
from subprocess import DEVNULL, STDOUT, check_output
from tempfile import NamedTemporaryFile
from zlib import compress, decompress


Question = namedtuple('Question', 'name description choices')
Answer = namedtuple('Answer', 'name choice')
MidiCmd = namedtuple('MidiCmd', 'track pos cmd vals')


class MidiFilter(object):
    def __init__(self, midifile):
        self.midifile = midifile

    def questions(self):
        raise NotImplementedError()

    def process(self, **answers):
        raise NotImplementedError()


class NoPanning(MidiFilter):
    """
    Removes the panning command because it may interfere with volume
    controls.
    """
    def has_panning(self):
        for i, midicmd in enumerate(self.midifile.data):
            if (midicmd.cmd == 'Control_c' and midicmd.vals[1] == '10' and
                    midicmd.vals[2] != '64'):
                return True
        return False

    def questions(self):
        if self.has_panning():
            return (
                Question(
                    name='nopan',
                    description='Do you wish to remove original panning?',
                    choices=((0, 'no'), (1, 'yes'))),
            )
        else:
            return ()

    def process(self, nopan=None, **answers):
        if nopan:
            self.midifile.data = list(filter(
                (lambda x: not (x.cmd == 'Control_c' and x.vals[1] == '10')),
                self.midifile.data))


class HighlightTrack(MidiFilter):
    """
    Reduces the volume of all tracks but the highlighted one.
    """
    def questions(self):
        choices = [(None, 'no nothing')]
        choices.extend(
            [(k, 'Highlight track {} {}'.format(k, v))
             for k, v in self.midifile.get_tracks().items()])
        return (
            Question(
                name='hltrack',
                description='Do you wish to highlight a track?',
                choices=choices),
            Question(
                name='hlpan',
                description='Do you want to pan your track to the right?',
                choices=((0, 'no'), (1, 'yes'))),
            Question(
                name='hlinv',
                description='Reduce volume of the highlighted track instead?',
                choices=((0, 'no'), (1, 'yes'))),
        )

    def process(self, hltrack=None, hlpan=None, hlinv=None, **answers):
        pan_insert = []

        if hltrack is not None:
            # Check that there is an initial volume that we can change.
            # For every Note_on-track-channel, we want a volume set.
            track_channels = {}
            for i, midicmd in enumerate(self.midifile.data):
                if midicmd.cmd == 'Note_on_c':
                    track_channel = (midicmd.track, midicmd.vals[0])
                    if track_channel not in track_channels:
                        track_channels[track_channel] = i
                elif midicmd.cmd == 'Control_c' and midicmd.vals[1] == '7':
                    track_channel = (midicmd.track, midicmd.vals[0])
                    if track_channel not in track_channels:
                        track_channels[track_channel] = True

            # Okay, all of them with track_channels as integer, have no
            # volume. Sort them reversed by line, so we can insert a bit
            # of volume.
            track_channels = [
                (v,) + k for k, v in track_channels.items() if v is not True]
            track_channels.sort(reverse=True)
            for i, track, channel in track_channels:
                # The previous line before the first Note_on_c is
                # probably still pos '0'.
                prev = self.midifile.data[i - 1]
                assert prev.track == track, (i, prev.track, track)
                self.midifile.data.insert(i, MidiCmd(
                    track=track,
                    pos=prev.pos,
                    cmd='Control_c',
                    vals=[channel, '7', '127']))

            # Second run, this time we update the volume.
            for i, midicmd in enumerate(self.midifile.data):
                if (midicmd.cmd == 'Control_c' and
                        midicmd.vals[1] == '7'):
                    is_selected = (midicmd.track == hltrack)

                    # Decrease volume if different track.
                    # (Or decrease is the highlighted track, and we're
                    # doing the inverse.)
                    if is_selected == hlinv:
                        assert len(midicmd.vals) == 3
                        new_volume = int(float(midicmd.vals[2]) * 0.35)
                        self.midifile.data[i] = midicmd._replace(
                            vals=[midicmd.vals[0], '7', str(new_volume)])

                    # Append panning info, possibly.
                    pan_value = ('0', '127')[is_selected]
                    pan_insert.append((i, midicmd._replace(
                        vals=[midicmd.vals[0], '10', pan_value])))

        if hlpan:
            # Append reversed, so we don't mess with the offsets.
            assert hltrack
            pan_insert.reverse()
            for i, midicmd in pan_insert:
                self.midifile.data.insert(i, midicmd)


class ReplaceInstruments(MidiFilter):
    """
    Replaces all instruments with 65 (
    Removes the panning command because it may interfere with volume.
    controls.
    """
    def has_instrument(self):
        for i, midicmd in enumerate(self.midifile.data):
            if midicmd.cmd == 'Program_c':
                return True
        return False

    def questions(self):
        if self.has_instrument():
            return (
                Question(
                    name='replinstr',
                    description=(
                        'Do you wish to replace instruments? (MIDI '
                        'instruments 1(piano)..128(gunshot))'),
                    choices=((0, 'no'), (1, 'Piano'), (66, 'Alt Sax'))),
            )
        else:
            return ()

    def process(self, replinstr=None, **answers):
        if replinstr:
            instr0 = str(replinstr - 1)  # 0-based 7-bit instrument number
            self.midifile.data = [
                MidiCmd(x.track, x.pos, x.cmd, [x.vals[0], instr0])
                if x.cmd == 'Program_c'
                else x
                for x in self.midifile.data]


class StripChords(MidiFilter):
    """
    Lets you strip chords and leave only one of the notes.
    (For instance, if you're the low bass, you only want the low note.)

    BUG: This filter does not take low notes into account that are started
    while a high note is still playing.
    """
    def find_chords_in_tracks(self):
        on = defaultdict(list)

        notes_by_pos_by_track = defaultdict(
            lambda: defaultdict(list))
        for midicmd in self.midifile.data:
            if midicmd.cmd == 'Note_on_c':
                notes_by_pos_by_track[midicmd.track][midicmd.pos].append(
                    int(midicmd.vals[1]))

        # Remove notes which only have one pos.
        for track, on in notes_by_pos_by_track.items():
            on = dict((k, v) for (k, v) in on.items() if len(v) > 1)
            notes_by_pos_by_track[track] = on

        # Remove the empty lists.
        notes_by_pos_by_track = dict(
            (k, v) for (k, v) in notes_by_pos_by_track.items() if v)

        return notes_by_pos_by_track

    def find_max_chord_sizes(self):
        ret = []
        chords = self.find_chords_in_tracks()
        for track, on in chords.items():
            max_per_track = max(len(i) for i in on.values())
            ret.append((track, max_per_track))
        ret.sort(key=(lambda x: x[0]))
        return ret

    def questions(self):
        choices = [(None, 'no nothing')]
        simultaneous_notes_per_track = self.find_max_chord_sizes()
        if not simultaneous_notes_per_track:
            return ()

        for track, n in simultaneous_notes_per_track:
            for i in range(n):
                choices.extend(
                    [('{}-{}'.format(track, i),
                      'Reduce chords in {} to the {}th lowest note'.format(
                          self.midifile.get_tracks()[track], i + 1))])

        return (
            Question(
                name='chordtone',
                description='Do you wish to turn chords into a single note?',
                choices=choices),
        )

    def process(self, chordtone=None, **answers):
        if chordtone:
            track, nth = [int(i) for i in chordtone.split('-')]

            # Take the nth lowest note only.
            on = self.find_chords_in_tracks()[track]
            on = dict((k, [n for i, n in enumerate(sorted(v)) if i == nth])
                      for (k, v) in on.items())

            # Alter midi file by removing the other chords. And mark
            # all Note_off_c that do not have a corresponding Note_on_c
            # for drop too.
            to_drop = []
            enabled_notes_for_track = dict()
            for i, midicmd in enumerate(self.midifile.data):
                if midicmd.track != track:
                    pass
                elif midicmd.cmd == 'Note_on_c':
                    tup = int(midicmd.vals[1])
                    if midicmd.pos in on.keys() and on[midicmd.pos] != [tup]:
                        to_drop.append(i)
                    else:
                        enabled_notes_for_track[tup] = True
                elif midicmd.cmd == 'Note_off_c':
                    tup = int(midicmd.vals[1])
                    if tup in enabled_notes_for_track:
                        del enabled_notes_for_track[tup]
                    else:
                        to_drop.append(i)

            # Pop backwards, so we don't interfere with the offsets.
            to_drop.reverse()
            for i in to_drop:
                self.midifile.data.pop(i)


class OutputFormat(MidiFilter):
    """
    Select output option: MID, MP3, CSV.
    """
    def questions(self):
        return (
            Question(
                name='fmt',
                description='What format should the output file have?',
                choices=(
                    (('mid', 'MIDI (.mid), the default'),
                     ('mp3', ('MP3 (.mp3), an audio file suitable for '
                              'playing on various devices '
                              '(takes up to a minute! be patient!)')),
                     ('csv', 'CSV (.csv), a comma separated text file')))),
        )

    def process(self, fmt=None, **answers):
        if fmt:
            self.midifile.set_default_outfmt(fmt)


class MidiFile(object):
    filters = (
        NoPanning,
        HighlightTrack,
        ReplaceInstruments,
        StripChords,
        OutputFormat,
    )

    def __init__(self):
        self.name = '(nameless)'
        self.cache = {}
        self.data = []
        self._default_outfmt = 'mid'

    @staticmethod
    def from_line(line):
        vals = line.strip().split(', ')
        return MidiCmd(
            track=int(vals[0]),
            pos=int(vals[1]),
            cmd=vals[2],
            vals=vals[3:])

    @staticmethod
    def to_line(midicmd):
        if midicmd.vals:
            return '{0.track}, {0.pos}, {0.cmd}, {1}\n'.format(
                midicmd, ', '.join(midicmd.vals))
        return '{0.track}, {0.pos}, {0.cmd}\n'.format(midicmd)

    def set_default_outfmt(self, fmt):
        self._default_outfmt = fmt

    def get_default_outfmt(self):
        content_type = {
            'csv': 'text/csv',
            'mid': 'audio/midi',
            'mp3': 'audio/mpeg',
        }[self._default_outfmt]
        return self._default_outfmt, content_type

    def export(self, fmt, outfile):
        return {
            'csv': self.export_csv,
            'mid': self.export_mid,
            'mp3': self.export_mp3,
        }[fmt](outfile)

    def export_csv(self, csvfile):
        for midicmd in self.data:
            line = self.to_line(midicmd).encode('utf-8')
            csvfile.write(line)

    def export_mid(self, midifile):
        try:
            midifile.fileno()
        except OSError:
            # Reload data onto midifile.
            with NamedTemporaryFile(mode='rb', suffix='.mid') as outfile:
                self._export_mid(outfile.name)
                midifile.write(outfile.read())
        else:
            self._export_mid(midifile.name)

    def _export_mid(self, midifilename):
        """
        Requires: csvmidi (midicsv package, see _load_mid)
        """
        with NamedTemporaryFile(mode='wb', suffix='.csv') as infile:
            self.export_csv(infile)
            infile.flush()
            check_output(['csvmidi', infile.name, midifilename])

    def export_mp3(self, mp3file):
        try:
            mp3file.fileno()
        except OSError:
            # Reload data onto mp3file.
            with NamedTemporaryFile(mode='rb', suffix='.mp3') as outfile:
                self._export_mp3(outfile.name)
                mp3file.write(outfile.read())
        else:
            self._export_mp3(mp3file.name)

    def _export_mp3(self, mp3file):
        """
        Convert to wav using timidity IN.mid -Ow -o OUT.mid. Note that we
        require Timidity 2.13.2+ with a bug fix for
        https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=693011
        (the fix_right_channel_crackle.patch -- x >= n in effect.c)

        Requires: manual-compiled-timidity
        Packages: fluid-soundfont-gm
        Config: /etc/timidity/timidity.cfg
        > source /etc/timidity/fluidr3_gm.cfg

        Then, convert to mp3, using sox with mp3 support (after first
        determining the normalization value).

        Requires: sox libsox-fmt-mp3
        """
        with NamedTemporaryFile(mode='rb', suffix='.wav') as wavfile:
            with NamedTemporaryFile(mode='rb', suffix='.mid') as midifile:
                self.export_mid(midifile)
                check_output(['timidity', midifile.name, '-Ow',
                              '-o', wavfile.name])
            volume = check_output(['sox', wavfile.name, '-n', 'stat', '-v'],
                                  stderr=STDOUT)
            volume = float(volume.strip())
            check_output(['sox', '-v', str(volume),
                          wavfile.name, mp3file], stderr=DEVNULL)

    def load_csv(self, csvfile):
        self.cache = {}
        try:
            self.data = [self.from_line(line.decode('latin1'))
                         for line in csvfile]
        except UnicodeDecodeError:
            self.data = [self.from_line(line.decode('utf-8', 'replace'))
                         for line in csvfile]

    def load_mid(self, midifile):
        try:
            midifile.fileno()
        except OSError:
            with NamedTemporaryFile(mode='wb', suffix='.mid') as infile:
                infile.write(midifile.read())
                self._load_mid(infile.name)
        else:
            self.name = midifile.name
            self._load_mid(midifile.name)

    def _load_mid(self, midifilename):
        """
        Requires midicsv-1.1 (or different version) with binaries
        midicsv/csvmidi.

        MIDI File CSV Editing Tools
        by John Walker
        http://www.fourmilab.ch/
        """
        with NamedTemporaryFile(mode='rb', suffix='.csv') as outfile:
            check_output(['midicsv', midifilename, outfile.name])
            self.load_csv(outfile)

    def get_tracks(self):
        if 'get_tracks' not in self.cache:
            self.cache['get_tracks'] = OrderedDict(
                (i.track, i.vals[0]) for i in self.data if i.cmd == 'Title_t')
        return self.cache['get_tracks']

    def questions(self):
        questions = []
        for filter_cls in self.filters:
            questions.extend(filter_cls(self).questions())
        return questions

    def process(self, answers):
        answers_as_dict = dict((i.name, i.choice) for i in answers)
        for filter_cls in self.filters:
            filter_cls(self).process(**answers_as_dict)


class CliShell(object):
    def __init__(self, midifile, infilename, outfilename):
        output_fmt = outfilename.rsplit('.', 1)[-1]
        assert output_fmt in ('csv', 'mid', 'mp3'), outfilename

        self.midifile = midifile
        self.infilename = infilename
        self.outfilename = outfilename

    def ask_questions(self):
        answers = []
        for question in self.midifile.questions():
            print(question.description)
            for i, choice in enumerate(question.choices):
                print('{}. {}'.format(i + 1, choice[1]))
            chosen = -1
            while not (1 <= chosen <= len(question.choices)):
                try:
                    chosen = int(input('Your choice? ').strip())
                except ValueError:
                    pass
            answers.append(Answer(
                name=question.name,
                choice=question.choices[chosen - 1][0]))
            print()
        return answers

    def process(self):
        print('Reading file {}'.format(self.infilename))
        with open(self.infilename, 'rb') as midifile:
            self.midifile.load_mid(midifile)
        print()

        answers = self.ask_questions()

        print('Processing file {}'.format(self.infilename))
        self.midifile.process(answers)
        print()

        print('Writing file {}'.format(self.outfilename))
        with open(self.outfilename, 'wb') as outfile:
            output_fmt = self.outfilename.rsplit('.', 1)[-1]
            self.midifile.export(output_fmt, outfile)
        print()


class CgiShell(object):
    def __init__(self, midifile, form, out):
        self.midifile = midifile
        self.form = form
        self.out = out
        self.started_output = False

    def write(self, msg):
        if not self.started_output:
            self.out.write('Content-Type: text/html; charset=utf-8\r\n\r\n')
            self.started_output = True
        self.out.write(msg)

    def process(self):
        if not self.form:
            self.page_upload()
        elif 'midifile' in self.form:
            self.page_questions()
        elif 'midicsv' in self.form:
            self.page_process()
        else:
            self.write(repr(self.form))

    def page_upload(self):
        self.write('<h1>Choir MID alterations: begin</h1>')
        self.write('''<form method="post" enctype="multipart/form-data">
            <p>Upload your midi file here:</p>
            <p><input type="file" name="midifile"/></p>
            <p><input type="submit"/></p>
        </form>
        ''')

    def page_questions(self):
        infile_name = self.form['midifile'].filename
        self.midifile.load_mid(BytesIO(self.form['midifile'].file.read()))

        # Compress the CSV file so we can reuse it after the questions.
        out = BytesIO()
        self.midifile.export_csv(out)
        out.seek(0)
        data = encodebytes(compress(out.read())).decode('ascii')

        # Create new form.
        self.write('<h1>Choir MID alterations: options</h1>\n')
        self.write('<em>{}</em>\n'.format(infile_name))
        self.write('<hr/>\n')
        self.write('''<form method="post">
            <input type="hidden" name="midifile_name" value="{midifile_name}"/>
            <input type="hidden" name="midicsv" value="{midicsv}"/>
        '''.format(
            midifile_name=infile_name, midicsv=data))
        for question in self.midifile.questions():
            self.write('''<p>{}</p><p>'''.format(question.description))
            for i, choice in enumerate(question.choices):
                extra = ' checked="checked"' if i == 0 else ''
                self.write('''
                    <input type="radio" name="{name}" id="{name}_{i}"
                     value="{value}"{extra}/>
                    <label for="{name}_{i}">{description}</label><br/>
                '''.format(
                    name=question.name, i=i, value=choice[0],
                    description=choice[1], extra=extra))
            self.write('</p><hr/>\n')
        self.write('''
            <p><input type="submit"/></p>
        </form>
        ''')

    def page_process(self):
        outfile_name = self.form.getfirst('midifile_name', 'output.mid')
        infile_gzipped = self.form.getfirst('midicsv', '')

        # Combine answers.
        answers = []
        for key in self.form.keys():
            if key not in ('midifile_name', 'midicsv'):
                choice = self.form.getfirst(key)
                if choice.isdigit():
                    choice = int(choice)
                else:
                    choice = {
                        'None': None,
                        'False': False,
                        'True': True,
                    }.get(choice, choice)
                answers.append(Answer(
                    name=key,
                    choice=choice))

        # Decompress the CSV file so we can load it.
        data = decompress(decodebytes(infile_gzipped.encode('ascii')))
        in_ = BytesIO(data)
        self.midifile.load_csv(in_)

        # Process, based on the answers.
        self.midifile.process(answers)

        # Create new filename based on settings and output selection.
        fmt, content_type = self.midifile.get_default_outfmt()
        outfile_head, outfile_tail = outfile_name.rsplit('.', 1)
        outfile_name = '{}_{}.{}'.format(
            outfile_head,
            '+'.join('{}={}'.format(i.name, i.choice) for i in sorted(answers)
                     if i.choice and i.name != 'fmt'),
            fmt)

        # Output as chosen output file type.
        out = BytesIO()
        self.midifile.export(fmt, out)
        size = out.tell()
        out.seek(0)
        self.out.write('Content-Type: {}\r\n'.format(content_type))
        self.out.write('Content-Length: {}\r\n'.format(size))
        self.out.write(
            'Content-Disposition: attachment; filename="{}"\r\n'.format(
                outfile_name))
        self.out.write('\r\n')
        self.out.flush()
        self.started_output = True
        self.out.buffer.write(out.read())  # for binary!


if __name__ == '__main__':
    midifile = MidiFile()
    if os.environ.get('GATEWAY_INTERFACE'):
        # Make sys.stdout utf-8 ready before creating exception trap hook.
        # #sys.stdout.reconfigure(encoding='utf-8')  # py3.7+
        buffer_ = sys.stdout.detach()
        sys.stdout = codecs.getwriter('utf8')(buffer_)
        sys.stdout.buffer = buffer_
        cgitb.enable()

        shell = CgiShell(midifile, cgi.FieldStorage(), sys.stdout)
        try:
            shell.process()
        except Exception:
            if not shell.started_output:
                sys.stdout.write(
                    'Content-Type: text/html; charset=utf-8\r\n\r\n')
                shell.started_output = True
            raise
    else:
        shell = CliShell(midifile, sys.argv[1], sys.argv[2])
        shell.process()
