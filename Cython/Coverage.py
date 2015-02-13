"""
A Cython plugin for coverage.py

Requires the coverage package at least in version 4.0 (which added the plugin API).
"""

import re
import os.path
from collections import defaultdict

try:
    from coverage.plugin import CoveragePlugin, FileTracer, FileReporter
except ImportError:
    # version too old?
    CoveragePlugin = FileTracer = FileReporter = object


from . import __version__


class Plugin(CoveragePlugin):
    _c_files_map = None

    def sys_info(self):
        return [('Cython version', __version__)]

    def file_tracer(self, filename):
        """
        Try to find a C source file for a file path found by the tracer.
        """
        c_file = py_file = None
        filename = os.path.abspath(filename)
        if self._c_files_map and filename in self._c_files_map:
            c_file = self._c_files_map[filename][0]

        if c_file is None:
            c_file, py_file = self._find_source_files(filename)
        if not c_file:
            return None

        try:
            with open(c_file, 'rb') as f:
                if b'/* Generated by Cython ' not in f.read(30):
                    return None  # not a Cython file
        except (IOError, OSError):
            return None

        # parse all source file paths and lines from C file
        # to learn about all relevant source files right away (pyx/pxi/pxd)
        # FIXME: this might already be too late if the first executed line
        #        is not from the main .pyx file but a file with a different
        #        name than the .c file (which prevents us from finding the
        #        .c file)
        self._parse_lines(c_file, filename)
        return CythonModuleTracer(filename, py_file, c_file, self._c_files_map)

    def file_reporter(self, filename):
        if os.path.splitext(filename)[1].lower() not in ('.pyx', '.pxi', '.pxd'):
            return None  # let coverage.py handle it (e.g. .py files)

        filename = os.path.abspath(filename)
        if not self._c_files_map or filename not in self._c_files_map:
            return None  # unknown file
        c_file, rel_file_path, code, excluded = self._c_files_map[filename]
        if code is None:
            return None  # unknown file
        return CythonModuleReporter(c_file, filename, rel_file_path, code, excluded)

    def _find_source_files(self, filename):
        basename, ext = os.path.splitext(filename)
        if ext.lower() not in ('.so', '.dll', '.c', '.cpp'):
            return None, None

        if os.path.exists(basename + '.c'):
            c_file = basename + '.c'
        elif os.path.exists(basename + '.cpp'):
            c_file = basename + '.cpp'
        else:
            c_file = None

        py_source_file = None
        if c_file:
            py_source_file = os.path.splitext(c_file)[0] + '.py'
            if not os.path.exists(py_source_file):
                py_source_file = None

        return c_file, py_source_file

    def _parse_lines(self, c_file, sourcefile):
        """
        Parse a Cython generated C/C++ source file and find the executable lines.
        Each executable line starts with a comment header that states source file
        and line number, as well as the surrounding range of source code lines.
        """
        match_source_path_line = re.compile(r' */[*] +"(.*)":([0-9]+)$').match
        match_current_code_line = re.compile(r' *[*] (.*) # <<<<<<+$').match
        match_comment_end = re.compile(r' *[*]/$').match

        code_lines = defaultdict(dict)
        max_line = defaultdict(int)
        filenames = set()
        with open(c_file) as lines:
            lines = iter(lines)
            for line in lines:
                match = match_source_path_line(line)
                if not match:
                    continue
                filename, lineno = match.groups()
                filenames.add(filename)
                lineno = int(lineno)
                max_line[filename] = max(max_line[filename], lineno)
                for comment_line in lines:
                    match = match_current_code_line(comment_line)
                    if match:
                        code_lines[filename][lineno] = match.group(1).rstrip()
                        break
                    elif match_comment_end(comment_line):
                        # unexpected comment format - false positive?
                        break

        excluded_lines = dict(
            (filename, set(range(1, max_line[filename] + 1)) - set(lines))
            for filename, lines in code_lines.iteritems()
        )

        if self._c_files_map is None:
            self._c_files_map = {}

        for filename in filenames:
            self._c_files_map[os.path.abspath(filename)] = (
                c_file, filename, code_lines[filename], excluded_lines[filename])

        if sourcefile not in self._c_files_map:
            return None, None  # shouldn't happen ...
        return self._c_files_map[sourcefile][1:]


class CythonModuleTracer(FileTracer):
    """
    Find the Python/Cython source file for a Cython module.
    """
    def __init__(self, module_file, py_file, c_file, c_files_map):
        super(CythonModuleTracer, self).__init__()
        self.module_file = module_file
        self.py_file = py_file
        self.c_file = c_file
        self._c_files_map = c_files_map

    def has_dynamic_source_filename(self):
        return True

    def dynamic_source_filename(self, filename, frame):
        source_file = frame.f_code.co_filename
        abs_path = os.path.abspath(source_file)

        if self.py_file and source_file.lower().endswith('.py'):
            # always let coverage.py handle this case itself
            return self.py_file

        assert self._c_files_map is not None
        if abs_path not in self._c_files_map:
            self._c_files_map[abs_path] = (self.c_file, source_file, None, None)
        return abs_path


class CythonModuleReporter(FileReporter):
    """
    Provide detailed trace information for one source file to coverage.py.
    """
    def __init__(self, c_file, source_file, rel_file_path, code, excluded):
        super(CythonModuleReporter, self).__init__(source_file)
        self.name = rel_file_path
        self.c_file = c_file
        self._code = code
        self._excluded = excluded

    def statements(self):
        return self._code.viewkeys()

    def excluded_statements(self):
        return self._excluded
