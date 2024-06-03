#!/usr/bin/env python3
import cgi
import cgitb
import os
import sys

from base64 import decodebytes, encodebytes
from collections import OrderedDict, defaultdict, namedtuple
from io import BytesIO
from subprocess import check_output
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
    Removes the panning command because it interferes.
    """
    def questions(self):
        # TODO: find panning, if there is none, then don't ask the question..
        return (
            Question(
                name='nopan',
                description='Do you wish to remove panning?',
                choices=((1, 'yes'), (0, 'no'))),
        )

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
        )

    def process(self, hltrack=None, hlpan=None, **answers):
        # TODO: insert initial volume if it doesn't exist?
        pan_insert = []
        if hltrack is not None:
            for i, midicmd in enumerate(self.midifile.data):
                if (midicmd.cmd == 'Control_c' and
                        midicmd.vals[1] == '7'):
                    is_selected = (midicmd.track == hltrack)

                    # Decrease volume if different track.
                    if not is_selected:
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
            pan_insert.reverse()
            for i, midicmd in pan_insert:
                self.midifile.data.insert(i, midicmd)


class StripChords(MidiFilter):
    """
    Lets you strip chords and leave only one of the notes.
    (For instance, if you're the low bass, you only want the low note.)
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


class MidiFile(object):
    filters = (
        NoPanning,
        HighlightTrack,
        StripChords,
    )

    def __init__(self):
        self.name = '(nameless)'
        self.cache = {}
        self.data = []

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

    def export(self, fmt, outfile):
        return {
            'csv': self.export_csv,
            'mid': self.export_mid,
            'mp3': self.export_mp3,
        }[fmt](outfile)

    def export_csv(self, csvfile):
        for midicmd in self.data:
            csvfile.write(self.to_line(midicmd).encode('ascii'))

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
        with NamedTemporaryFile(mode='wb', suffix='.csv') as infile:
            self.export_csv(infile)
            infile.flush()
            check_output(['csvmidi', infile.name, midifilename])

    def export_mp3(self, mp3file):
        raise NotImplementedError()

    def load_csv(self, csvfile):
        self.cache = {}
        self.data = [self.from_line(line.decode('ascii', 'replace'))
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
        with open(self.infilename) as midifile:
            self.midifile.load_mid(midifile)
        print()

        answers = self.ask_questions()

        print('Processing file {}'.format(self.infilename))
        self.midifile.process(answers)
        print()

        print('Writing file {}'.format(self.outfilename))
        with open(self.outfilename, 'w') as outfile:
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
            self.out.write('Content-Type: text/html\r\n\r\n')
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
        outfile_head, outfile_tail = outfile_name.rsplit('.', 1)
        outfile_name = '{}_{}.{}'.format(
            outfile_head,
            '+'.join('{}={}'.format(i.name, i.choice) for i in sorted(answers)
                     if i.choice),
            outfile_tail)

        # Output as midi file.
        out = BytesIO()
        self.midifile.export_mid(out)
        size = out.tell()
        out.seek(0)
        self.out.write('Content-Type: audio/midi\r\n')
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
        cgitb.enable()
        shell = CgiShell(midifile, cgi.FieldStorage(), sys.stdout)
    else:
        shell = CliShell(midifile, sys.argv[1], sys.argv[2])
    shell.process()