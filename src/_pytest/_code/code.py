import ast
import dataclasses
import inspect
import os
import re
import sys
import traceback
from inspect import CO_VARARGS
from inspect import CO_VARKEYWORDS
from io import StringIO
from pathlib import Path
from traceback import format_exception_only
from types import CodeType
from types import FrameType
from types import TracebackType
from typing import Any
from typing import Callable
from typing import ClassVar
from typing import Dict
from typing import Generic
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import overload
from typing import Pattern
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import Type
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union
from weakref import ref

import pluggy

import _pytest
from _pytest._code.source import findsource
from _pytest._code.source import getrawcode
from _pytest._code.source import getstatementrange_ast
from _pytest._code.source import Source
from _pytest._io import TerminalWriter
from _pytest._io.saferepr import safeformat
from _pytest._io.saferepr import saferepr
from _pytest.compat import final
from _pytest.compat import get_real_func
from _pytest.deprecated import check_ispytest
from _pytest.pathlib import absolutepath
from _pytest.pathlib import bestrelpath

if TYPE_CHECKING:
    from typing_extensions import Literal
    from typing_extensions import SupportsIndex
    from weakref import ReferenceType

    _TracebackStyle = Literal["long", "short", "line", "no", "native", "value", "auto"]

if sys.version_info[:2] < (3, 11):
    from exceptiongroup import BaseExceptionGroup


class Code:
    """Wrapper around Python code objects."""

    __slots__ = ("raw",)

    def __init__(self, obj: CodeType) -> None:
        self.raw = obj

    @classmethod
    def from_function(cls, obj: object) -> "Code":
        return cls(getrawcode(obj))

    def __eq__(self, other):
        return self.raw == other.raw

    # Ignore type because of https://github.com/python/mypy/issues/4266.
    __hash__ = None  # type: ignore

    @property
    def firstlineno(self) -> int:
        return self.raw.co_firstlineno - 1

    @property
    def name(self) -> str:
        return self.raw.co_name

    @property
    def path(self) -> Union[Path, str]:
        """Return a path object pointing to source code, or an ``str`` in
        case of ``OSError`` / non-existing file."""
        if not self.raw.co_filename:
            return ""
        try:
            p = absolutepath(self.raw.co_filename)
            # maybe don't try this checking
            if not p.exists():
                raise OSError("path check failed.")
            return p
        except OSError:
            # XXX maybe try harder like the weird logic
            # in the standard lib [linecache.updatecache] does?
            return self.raw.co_filename

    @property
    def fullsource(self) -> Optional["Source"]:
        """Return a _pytest._code.Source object for the full source file of the code."""
        full, _ = findsource(self.raw)
        return full

    def source(self) -> "Source":
        """Return a _pytest._code.Source object for the code object's source only."""
        # return source only for that part of code
        return Source(self.raw)

    def getargs(self, var: bool = False) -> Tuple[str, ...]:
        """Return a tuple with the argument names for the code object.

        If 'var' is set True also return the names of the variable and
        keyword arguments when present.
        """
        # Handy shortcut for getting args.
        raw = self.raw
        argcount = raw.co_argcount
        if var:
            argcount += raw.co_flags & CO_VARARGS
            argcount += raw.co_flags & CO_VARKEYWORDS
        return raw.co_varnames[:argcount]


class Frame:
    """Wrapper around a Python frame holding f_locals and f_globals
    in which expressions can be evaluated."""

    __slots__ = ("raw",)

    def __init__(self, frame: FrameType) -> None:
        self.raw = frame

    @property
    def lineno(self) -> int:
        return self.raw.f_lineno - 1

    @property
    def f_globals(self) -> Dict[str, Any]:
        return self.raw.f_globals

    @property
    def f_locals(self) -> Dict[str, Any]:
        return self.raw.f_locals

    @property
    def code(self) -> Code:
        return Code(self.raw.f_code)

    @property
    def statement(self) -> "Source":
        """Statement this frame is at."""
        if self.code.fullsource is None:
            return Source("")
        return self.code.fullsource.getstatement(self.lineno)

    def eval(self, code, **vars):
        """Evaluate 'code' in the frame.

        'vars' are optional additional local variables.

        Returns the result of the evaluation.
        """
        f_locals = self.f_locals.copy()
        f_locals.update(vars)
        return eval(code, self.f_globals, f_locals)

    def repr(self, object: object) -> str:
        """Return a 'safe' (non-recursive, one-line) string repr for 'object'."""
        return saferepr(object)

    def getargs(self, var: bool = False):
        """Return a list of tuples (name, value) for all arguments.

        If 'var' is set True, also include the variable and keyword arguments
        when present.
        """
        retval = []
        for arg in self.code.getargs(var):
            try:
                retval.append((arg, self.f_locals[arg]))
            except KeyError:
                pass  # this can occur when using Psyco
        return retval


class TracebackEntry:
    """A single entry in a Traceback."""

    __slots__ = ("_rawentry", "_excinfo", "_repr_style")

    def __init__(
        self,
        rawentry: TracebackType,
        excinfo: Optional["ReferenceType[ExceptionInfo[BaseException]]"] = None,
    ) -> None:
        self._rawentry = rawentry
        self._excinfo = excinfo
        self._repr_style: Optional['Literal["short", "long"]'] = None

    @property
    def lineno(self) -> int:
        return self._rawentry.tb_lineno - 1

    def set_repr_style(self, mode: "Literal['short', 'long']") -> None:
        assert mode in ("short", "long")
        self._repr_style = mode

    @property
    def frame(self) -> Frame:
        return Frame(self._rawentry.tb_frame)

    @property
    def relline(self) -> int:
        return self.lineno - self.frame.code.firstlineno

    def __repr__(self) -> str:
        return "<TracebackEntry %s:%d>" % (self.frame.code.path, self.lineno + 1)

    @property
    def statement(self) -> "Source":
        """_pytest._code.Source object for the current statement."""
        source = self.frame.code.fullsource
        assert source is not None
        return source.getstatement(self.lineno)

    @property
    def path(self) -> Union[Path, str]:
        """Path to the source code."""
        return self.frame.code.path

    @property
    def locals(self) -> Dict[str, Any]:
        """Locals of underlying frame."""
        return self.frame.f_locals

    def getfirstlinesource(self) -> int:
        return self.frame.code.firstlineno

    def getsource(
        self, astcache: Optional[Dict[Union[str, Path], ast.AST]] = None
    ) -> Optional["Source"]:
        """Return failing source code."""
        # we use the passed in astcache to not reparse asttrees
        # within exception info printing
        source = self.frame.code.fullsource
        if source is None:
            return None
        key = astnode = None
        if astcache is not None:
            key = self.frame.code.path
            if key is not None:
                astnode = astcache.get(key, None)
        start = self.getfirstlinesource()
        try:
            astnode, _, end = getstatementrange_ast(
                self.lineno, source, astnode=astnode
            )
        except SyntaxError:
            end = self.lineno + 1
        else:
            if key is not None and astcache is not None:
                astcache[key] = astnode
        return source[start:end]

    source = property(getsource)

    def ishidden(self) -> bool:
        """Return True if the current frame has a var __tracebackhide__
        resolving to True.

        If __tracebackhide__ is a callable, it gets called with the
        ExceptionInfo instance and can decide whether to hide the traceback.

        Mostly for internal use.
        """
        tbh: Union[
            bool, Callable[[Optional[ExceptionInfo[BaseException]]], bool]
        ] = False
        for maybe_ns_dct in (self.frame.f_locals, self.frame.f_globals):
            # in normal cases, f_locals and f_globals are dictionaries
            # however via `exec(...)` / `eval(...)` they can be other types
            # (even incorrect types!).
            # as such, we suppress all exceptions while accessing __tracebackhide__
            try:
                tbh = maybe_ns_dct["__tracebackhide__"]
            except Exception:
                pass
            else:
                break
        if tbh and callable(tbh):
            return tbh(None if self._excinfo is None else self._excinfo())
        return tbh

    def __str__(self) -> str:
        name = self.frame.code.name
        try:
            line = str(self.statement).lstrip()
        except KeyboardInterrupt:
            raise
        except BaseException:
            line = "???"
        # This output does not quite match Python's repr for traceback entries,
        # but changing it to do so would break certain plugins.  See
        # https://github.com/pytest-dev/pytest/pull/7535/ for details.
        return "  File %r:%d in %s\n  %s\n" % (
            str(self.path),
            self.lineno + 1,
            name,
            line,
        )

    @property
    def name(self) -> str:
        """co_name of underlying code."""
        return self.frame.code.raw.co_name


class Traceback(List[TracebackEntry]):
    """Traceback objects encapsulate and offer higher level access to Traceback entries."""

    def __init__(
        self,
        tb: Union[TracebackType, Iterable[TracebackEntry]],
        excinfo: Optional["ReferenceType[ExceptionInfo[BaseException]]"] = None,
    ) -> None:
        """Initialize from given python traceback object and ExceptionInfo."""
        self._excinfo = excinfo
        if isinstance(tb, TracebackType):

            def f(cur: TracebackType) -> Iterable[TracebackEntry]:
                cur_: Optional[TracebackType] = cur
                while cur_ is not None:
                    yield TracebackEntry(cur_, excinfo=excinfo)
                    cur_ = cur_.tb_next

            super().__init__(f(tb))
        else:
            super().__init__(tb)

    def cut(
        self,
        path: Optional[Union["os.PathLike[str]", str]] = None,
        lineno: Optional[int] = None,
        firstlineno: Optional[int] = None,
        excludepath: Optional["os.PathLike[str]"] = None,
    ) -> "Traceback":
        """Return a Traceback instance wrapping part of this Traceback.

        By providing any combination of path, lineno and firstlineno, the
        first frame to start the to-be-returned traceback is determined.

        This allows cutting the first part of a Traceback instance e.g.
        for formatting reasons (removing some uninteresting bits that deal
        with handling of the exception/traceback).
        """
        path_ = None if path is None else os.fspath(path)
        excludepath_ = None if excludepath is None else os.fspath(excludepath)
        for x in self:
            code = x.frame.code
            codepath = code.path
            if path is not None and str(codepath) != path_:
                continue
            if (
                excludepath is not None
                and isinstance(codepath, Path)
                and excludepath_ in (str(p) for p in codepath.parents)  # type: ignore[operator]
            ):
                continue
            if lineno is not None and x.lineno != lineno:
                continue
            if firstlineno is not None and x.frame.code.firstlineno != firstlineno:
                continue
            return Traceback(x._rawentry, self._excinfo)
        return self

    @overload
    def __getitem__(self, key: "SupportsIndex") -> TracebackEntry:
        ...

    @overload
    def __getitem__(self, key: slice) -> "Traceback":
        ...

    def __getitem__(
        self, key: Union["SupportsIndex", slice]
    ) -> Union[TracebackEntry, "Traceback"]:
        if isinstance(key, slice):
            return self.__class__(super().__getitem__(key))
        else:
            return super().__getitem__(key)

    def filter(
        self, fn: Callable[[TracebackEntry], bool] = lambda x: not x.ishidden()
    ) -> "Traceback":
        """Return a Traceback instance with certain items removed

        fn is a function that gets a single argument, a TracebackEntry
        instance, and should return True when the item should be added
        to the Traceback, False when not.

        By default this removes all the TracebackEntries which are hidden
        (see ishidden() above).
        """
        return Traceback(filter(fn, self), self._excinfo)

    def getcrashentry(self) -> Optional[TracebackEntry]:
        """Return last non-hidden traceback entry that lead to the exception of a traceback."""
        for i in range(-1, -len(self) - 1, -1):
            entry = self[i]
            if not entry.ishidden():
                return entry
        return None

    def recursionindex(self) -> Optional[int]:
        """Return the index of the frame/TracebackEntry where recursion originates if
        appropriate, None if no recursion occurred."""
        cache: Dict[Tuple[Any, int, int], List[Dict[str, Any]]] = {}
        for i, entry in enumerate(self):
            # id for the code.raw is needed to work around
            # the strange metaprogramming in the decorator lib from pypi
            # which generates code objects that have hash/value equality
            # XXX needs a test
            key = entry.frame.code.path, id(entry.frame.code.raw), entry.lineno
            # print "checking for recursion at", key
            values = cache.setdefault(key, [])
            if values:
                f = entry.frame
                loc = f.f_locals
                for otherloc in values:
                    if otherloc == loc:
                        return i
            values.append(entry.frame.f_locals)
        return None


E = TypeVar("E", bound=BaseException, covariant=True)


@final
@dataclasses.dataclass
class ExceptionInfo(Generic[E]):
    """Wraps sys.exc_info() objects and offers help for navigating the traceback."""

    _assert_start_repr: ClassVar = "AssertionError('assert "

    _excinfo: Optional[Tuple[Type["E"], "E", TracebackType]]
    _striptext: str
    _traceback: Optional[Traceback]

    def __init__(
        self,
        excinfo: Optional[Tuple[Type["E"], "E", TracebackType]],
        striptext: str = "",
        traceback: Optional[Traceback] = None,
        *,
        _ispytest: bool = False,
    ) -> None:
        check_ispytest(_ispytest)
        self._excinfo = excinfo
        self._striptext = striptext
        self._traceback = traceback

    @classmethod
    def from_exc_info(
        cls,
        exc_info: Tuple[Type[E], E, TracebackType],
        exprinfo: Optional[str] = None,
    ) -> "ExceptionInfo[E]":
        """Return an ExceptionInfo for an existing exc_info tuple.

        .. warning::

            Experimental API

        :param exprinfo:
            A text string helping to determine if we should strip
            ``AssertionError`` from the output. Defaults to the exception
            message/``__str__()``.
        """
        _striptext = ""
        if exprinfo is None and isinstance(exc_info[1], AssertionError):
            exprinfo = getattr(exc_info[1], "msg", None)
            if exprinfo is None:
                exprinfo = saferepr(exc_info[1])
            if exprinfo and exprinfo.startswith(cls._assert_start_repr):
                _striptext = "AssertionError: "

        return cls(exc_info, _striptext, _ispytest=True)

    @classmethod
    def from_current(
        cls, exprinfo: Optional[str] = None
    ) -> "ExceptionInfo[BaseException]":
        """Return an ExceptionInfo matching the current traceback.

        .. warning::

            Experimental API

        :param exprinfo:
            A text string helping to determine if we should strip
            ``AssertionError`` from the output. Defaults to the exception
            message/``__str__()``.
        """
        tup = sys.exc_info()
        assert tup[0] is not None, "no current exception"
        assert tup[1] is not None, "no current exception"
        assert tup[2] is not None, "no current exception"
        exc_info = (tup[0], tup[1], tup[2])
        return ExceptionInfo.from_exc_info(exc_info, exprinfo)

    @classmethod
    def for_later(cls) -> "ExceptionInfo[E]":
        """Return an unfilled ExceptionInfo."""
        return cls(None, _ispytest=True)

    def fill_unfilled(self, exc_info: Tuple[Type[E], E, TracebackType]) -> None:
        """Fill an unfilled ExceptionInfo created with ``for_later()``."""
        assert self._excinfo is None, "ExceptionInfo was already filled"
        self._excinfo = exc_info

    @property
    def type(self) -> Type[E]:
        """The exception class."""
        assert (
            self._excinfo is not None
        ), ".type can only be used after the context manager exits"
        return self._excinfo[0]

    @property
    def value(self) -> E:
        """The exception value."""
        assert (
            self._excinfo is not None
        ), ".value can only be used after the context manager exits"
        return self._excinfo[1]

    @property
    def tb(self) -> TracebackType:
        """The exception raw traceback."""
        assert (
            self._excinfo is not None
        ), ".tb can only be used after the context manager exits"
        return self._excinfo[2]

    @property
    def typename(self) -> str:
        """The type name of the exception."""
        assert (
            self._excinfo is not None
        ), ".typename can only be used after the context manager exits"
        return self.type.__name__

    @property
    def traceback(self) -> Traceback:
        """The traceback."""
        if self._traceback is None:
            self._traceback = Traceback(self.tb, excinfo=ref(self))
        return self._traceback

    @traceback.setter
    def traceback(self, value: Traceback) -> None:
        self._traceback = value

    def __repr__(self) -> str:
        if self._excinfo is None:
            return "<ExceptionInfo for raises contextmanager>"
        return "<{} {} tblen={}>".format(
            self.__class__.__name__, saferepr(self._excinfo[1]), len(self.traceback)
        )

    def exconly(self, tryshort: bool = False) -> str:
        """Return the exception as a string.

        When 'tryshort' resolves to True, and the exception is an
        AssertionError, only the actual exception part of the exception
        representation is returned (so 'AssertionError: ' is removed from
        the beginning).
        """
        lines = format_exception_only(self.type, self.value)
        text = "".join(lines)
        text = text.rstrip()
        if tryshort:
            if text.startswith(self._striptext):
                text = text[len(self._striptext) :]
        return text

    def errisinstance(
        self, exc: Union[Type[BaseException], Tuple[Type[BaseException], ...]]
    ) -> bool:
        """Return True if the exception is an instance of exc.

        Consider using ``isinstance(excinfo.value, exc)`` instead.
        """
        return isinstance(self.value, exc)

    def _getreprcrash(self) -> Optional["ReprFileLocation"]:
        exconly = self.exconly(tryshort=True)
        entry = self.traceback.getcrashentry()
        if entry:
            path, lineno = entry.frame.code.raw.co_filename, entry.lineno
            return ReprFileLocation(path, lineno + 1, exconly)
        return None

    def getrepr(
        self,
        showlocals: bool = False,
        style: "_TracebackStyle" = "long",
        abspath: bool = False,
        tbfilter: bool = True,
        funcargs: bool = False,
        truncate_locals: bool = True,
        chain: bool = True,
    ) -> Union["ReprExceptionInfo", "ExceptionChainRepr"]:
        """Return str()able representation of this exception info.

        :param bool showlocals:
            Show locals per traceback entry.
            Ignored if ``style=="native"``.

        :param str style:
            long|short|line|no|native|value traceback style.

        :param bool abspath:
            If paths should be changed to absolute or left unchanged.

        :param bool tbfilter:
            Hide entries that contain a local variable ``__tracebackhide__==True``.
            Ignored if ``style=="native"``.

        :param bool funcargs:
            Show fixtures ("funcargs" for legacy purposes) per traceback entry.

        :param bool truncate_locals:
            With ``showlocals==True``, make sure locals can be safely represented as strings.

        :param bool chain:
            If chained exceptions in Python 3 should be shown.

        .. versionchanged:: 3.9

            Added the ``chain`` parameter.
        """
        if style == "native":
            return ReprExceptionInfo(
                reprtraceback=ReprTracebackNative(
                    traceback.format_exception(
                        self.type, self.value, self.traceback[0]._rawentry
                    )
                ),
                reprcrash=self._getreprcrash(),
            )

        fmt = FormattedExcinfo(
            showlocals=showlocals,
            style=style,
            abspath=abspath,
            tbfilter=tbfilter,
            funcargs=funcargs,
            truncate_locals=truncate_locals,
            chain=chain,
        )
        return fmt.repr_excinfo(self)

    def match(self, regexp: Union[str, Pattern[str]]) -> "Literal[True]":
        """Check whether the regular expression `regexp` matches the string
        representation of the exception using :func:`python:re.search`.

        If it matches `True` is returned, otherwise an `AssertionError` is raised.
        """
        __tracebackhide__ = True
        value = str(self.value)
        msg = f"Regex pattern did not match.\n Regex: {regexp!r}\n Input: {value!r}"
        if regexp == value:
            msg += "\n Did you mean to `re.escape()` the regex?"
        assert re.search(regexp, value), msg
        # Return True to allow for "assert excinfo.match()".
        return True


@dataclasses.dataclass
class FormattedExcinfo:
    """Presenting information about failing Functions and Generators."""

    # for traceback entries
    flow_marker: ClassVar = ">"
    fail_marker: ClassVar = "E"

    showlocals: bool = False
    style: "_TracebackStyle" = "long"
    abspath: bool = True
    tbfilter: bool = True
    funcargs: bool = False
    truncate_locals: bool = True
    chain: bool = True
    astcache: Dict[Union[str, Path], ast.AST] = dataclasses.field(
        default_factory=dict, init=False, repr=False
    )

    def _getindent(self, source: "Source") -> int:
        # Figure out indent for the given source.
        try:
            s = str(source.getstatement(len(source) - 1))
        except KeyboardInterrupt:
            raise
        except BaseException:
            try:
                s = str(source[-1])
            except KeyboardInterrupt:
                raise
            except BaseException:
                return 0
        return 4 + (len(s) - len(s.lstrip()))

    def _getentrysource(self, entry: TracebackEntry) -> Optional["Source"]:
        source = entry.getsource(self.astcache)
        if source is not None:
            source = source.deindent()
        return source

    def repr_args(self, entry: TracebackEntry) -> Optional["ReprFuncArgs"]:
        if self.funcargs:
            args = []
            for argname, argvalue in entry.frame.getargs(var=True):
                args.append((argname, saferepr(argvalue)))
            return ReprFuncArgs(args)
        return None

    def get_source(
        self,
        source: Optional["Source"],
        line_index: int = -1,
        excinfo: Optional[ExceptionInfo[BaseException]] = None,
        short: bool = False,
    ) -> List[str]:
        """Return formatted and marked up source lines."""
        lines = []
        if source is not None and line_index < 0:
            line_index += len(source)
        if source is None or line_index >= len(source.lines) or line_index < 0:
            # `line_index` could still be outside `range(len(source.lines))` if
            # we're processing AST with pathological position attributes.
            source = Source("???")
            line_index = 0
        space_prefix = "    "
        if short:
            lines.append(space_prefix + source.lines[line_index].strip())
        else:
            for line in source.lines[:line_index]:
                lines.append(space_prefix + line)
            lines.append(self.flow_marker + "   " + source.lines[line_index])
            for line in source.lines[line_index + 1 :]:
                lines.append(space_prefix + line)
        if excinfo is not None:
            indent = 4 if short else self._getindent(source)
            lines.extend(self.get_exconly(excinfo, indent=indent, markall=True))
        return lines

    def get_exconly(
        self,
        excinfo: ExceptionInfo[BaseException],
        indent: int = 4,
        markall: bool = False,
    ) -> List[str]:
        lines = []
        indentstr = " " * indent
        # Get the real exception information out.
        exlines = excinfo.exconly(tryshort=True).split("\n")
        failindent = self.fail_marker + indentstr[1:]
        for line in exlines:
            lines.append(failindent + line)
            if not markall:
                failindent = indentstr
        return lines

    def repr_locals(self, locals: Mapping[str, object]) -> Optional["ReprLocals"]:
        if self.showlocals:
            lines = []
            keys = [loc for loc in locals if loc[0] != "@"]
            keys.sort()
            for name in keys:
                value = locals[name]
                if name == "__builtins__":
                    lines.append("__builtins__ = <builtins>")
                else:
                    # This formatting could all be handled by the
                    # _repr() function, which is only reprlib.Repr in
                    # disguise, so is very configurable.
                    if self.truncate_locals:
                        str_repr = saferepr(value)
                    else:
                        str_repr = safeformat(value)
                    # if len(str_repr) < 70 or not isinstance(value, (list, tuple, dict)):
                    lines.append(f"{name:<10} = {str_repr}")
                    # else:
                    #    self._line("%-10s =\\" % (name,))
                    #    # XXX
                    #    pprint.pprint(value, stream=self.excinfowriter)
            return ReprLocals(lines)
        return None

    def repr_traceback_entry(
        self,
        entry: TracebackEntry,
        excinfo: Optional[ExceptionInfo[BaseException]] = None,
    ) -> "ReprEntry":
        lines: List[str] = []
        style = entry._repr_style if entry._repr_style is not None else self.style
        if style in ("short", "long"):
            source = self._getentrysource(entry)
            if source is None:
                source = Source("???")
                line_index = 0
            else:
                line_index = entry.lineno - entry.getfirstlinesource()
            short = style == "short"
            reprargs = self.repr_args(entry) if not short else None
            s = self.get_source(source, line_index, excinfo, short=short)
            lines.extend(s)
            if short:
                message = "in %s" % (entry.name)
            else:
                message = excinfo and excinfo.typename or ""
            entry_path = entry.path
            path = self._makepath(entry_path)
            reprfileloc = ReprFileLocation(path, entry.lineno + 1, message)
            localsrepr = self.repr_locals(entry.locals)
            return ReprEntry(lines, reprargs, localsrepr, reprfileloc, style)
        elif style == "value":
            if excinfo:
                lines.extend(str(excinfo.value).split("\n"))
            return ReprEntry(lines, None, None, None, style)
        else:
            if excinfo:
                lines.extend(self.get_exconly(excinfo, indent=4))
            return ReprEntry(lines, None, None, None, style)

    def _makepath(self, path: Union[Path, str]) -> str:
        if not self.abspath and isinstance(path, Path):
            try:
                np = bestrelpath(Path.cwd(), path)
            except OSError:
                return str(path)
            if len(np) < len(str(path)):
                return np
        return str(path)

    def repr_traceback(self, excinfo: ExceptionInfo[BaseException]) -> "ReprTraceback":
        traceback = excinfo.traceback
        if self.tbfilter:
            traceback = traceback.filter()

        if isinstance(excinfo.value, RecursionError):
            traceback, extraline = self._truncate_recursive_traceback(traceback)
        else:
            extraline = None

        last = traceback[-1]
        entries = []
        if self.style == "value":
            reprentry = self.repr_traceback_entry(last, excinfo)
            entries.append(reprentry)
            return ReprTraceback(entries, None, style=self.style)

        for index, entry in enumerate(traceback):
            einfo = (last == entry) and excinfo or None
            reprentry = self.repr_traceback_entry(entry, einfo)
            entries.append(reprentry)
        return ReprTraceback(entries, extraline, style=self.style)

    def _truncate_recursive_traceback(
        self, traceback: Traceback
    ) -> Tuple[Traceback, Optional[str]]:
        """Truncate the given recursive traceback trying to find the starting
        point of the recursion.

        The detection is done by going through each traceback entry and
        finding the point in which the locals of the frame are equal to the
        locals of a previous frame (see ``recursionindex()``).

        Handle the situation where the recursion process might raise an
        exception (for example comparing numpy arrays using equality raises a
        TypeError), in which case we do our best to warn the user of the
        error and show a limited traceback.
        """
        try:
            recursionindex = traceback.recursionindex()
        except Exception as e:
            max_frames = 10
            extraline: Optional[str] = (
                "!!! Recursion error detected, but an error occurred locating the origin of recursion.\n"
                "  The following exception happened when comparing locals in the stack frame:\n"
                "    {exc_type}: {exc_msg}\n"
                "  Displaying first and last {max_frames} stack frames out of {total}."
            ).format(
                exc_type=type(e).__name__,
                exc_msg=str(e),
                max_frames=max_frames,
                total=len(traceback),
            )
            # Type ignored because adding two instances of a List subtype
            # currently incorrectly has type List instead of the subtype.
            traceback = traceback[:max_frames] + traceback[-max_frames:]  # type: ignore
        else:
            if recursionindex is not None:
                extraline = "!!! Recursion detected (same locals & position)"
                traceback = traceback[: recursionindex + 1]
            else:
                extraline = None

        return traceback, extraline

    def repr_excinfo(
        self, excinfo: ExceptionInfo[BaseException]
    ) -> "ExceptionChainRepr":
        repr_chain: List[
            Tuple[ReprTraceback, Optional[ReprFileLocation], Optional[str]]
        ] = []
        e: Optional[BaseException] = excinfo.value
        excinfo_: Optional[ExceptionInfo[BaseException]] = excinfo
        descr = None
        seen: Set[int] = set()
        while e is not None and id(e) not in seen:
            seen.add(id(e))
            if excinfo_:
                # Fall back to native traceback as a temporary workaround until
                # full support for exception groups added to ExceptionInfo.
                # See https://github.com/pytest-dev/pytest/issues/9159
                if isinstance(e, BaseExceptionGroup):
                    reprtraceback: Union[
                        ReprTracebackNative, ReprTraceback
                    ] = ReprTracebackNative(
                        traceback.format_exception(
                            type(excinfo_.value),
                            excinfo_.value,
                            excinfo_.traceback[0]._rawentry,
                        )
                    )
                else:
                    reprtraceback = self.repr_traceback(excinfo_)

                # will be None if all traceback entries are hidden
                reprcrash: Optional[ReprFileLocation] = excinfo_._getreprcrash()
                if reprcrash:
                    if self.style == "value":
                        repr_chain += [(reprtraceback, None, descr)]
                    else:
                        repr_chain += [(reprtraceback, reprcrash, descr)]
            else:
                # Fallback to native repr if the exception doesn't have a traceback:
                # ExceptionInfo objects require a full traceback to work.
                reprtraceback = ReprTracebackNative(
                    traceback.format_exception(type(e), e, None)
                )
                reprcrash = None
                repr_chain += [(reprtraceback, reprcrash, descr)]

            if e.__cause__ is not None and self.chain:
                e = e.__cause__
                excinfo_ = (
                    ExceptionInfo.from_exc_info((type(e), e, e.__traceback__))
                    if e.__traceback__
                    else None
                )
                descr = "The above exception was the direct cause of the following exception:"
            elif (
                e.__context__ is not None and not e.__suppress_context__ and self.chain
            ):
                e = e.__context__
                excinfo_ = (
                    ExceptionInfo.from_exc_info((type(e), e, e.__traceback__))
                    if e.__traceback__
                    else None
                )
                descr = "During handling of the above exception, another exception occurred:"
            else:
                e = None
        repr_chain.reverse()
        return ExceptionChainRepr(repr_chain)


@dataclasses.dataclass(eq=False)
class TerminalRepr:
    def __str__(self) -> str:
        # FYI this is called from pytest-xdist's serialization of exception
        # information.
        io = StringIO()
        tw = TerminalWriter(file=io)
        self.toterminal(tw)
        return io.getvalue().strip()

    def __repr__(self) -> str:
        return f"<{self.__class__} instance at {id(self):0x}>"

    def toterminal(self, tw: TerminalWriter) -> None:
        raise NotImplementedError()


# This class is abstract -- only subclasses are instantiated.
@dataclasses.dataclass(eq=False)
class ExceptionRepr(TerminalRepr):
    # Provided by subclasses.
    reprtraceback: "ReprTraceback"
    reprcrash: Optional["ReprFileLocation"]
    sections: List[Tuple[str, str, str]] = dataclasses.field(
        init=False, default_factory=list
    )

    def addsection(self, name: str, content: str, sep: str = "-") -> None:
        self.sections.append((name, content, sep))

    def toterminal(self, tw: TerminalWriter) -> None:
        for name, content, sep in self.sections:
            tw.sep(sep, name)
            tw.line(content)


@dataclasses.dataclass(eq=False)
class ExceptionChainRepr(ExceptionRepr):
    chain: Sequence[Tuple["ReprTraceback", Optional["ReprFileLocation"], Optional[str]]]

    def __init__(
        self,
        chain: Sequence[
            Tuple["ReprTraceback", Optional["ReprFileLocation"], Optional[str]]
        ],
    ) -> None:
        # reprcrash and reprtraceback of the outermost (the newest) exception
        # in the chain.
        super().__init__(
            reprtraceback=chain[-1][0],
            reprcrash=chain[-1][1],
        )
        self.chain = chain

    def toterminal(self, tw: TerminalWriter) -> None:
        for element in self.chain:
            element[0].toterminal(tw)
            if element[2] is not None:
                tw.line("")
                tw.line(element[2], yellow=True)
        super().toterminal(tw)


@dataclasses.dataclass(eq=False)
class ReprExceptionInfo(ExceptionRepr):
    reprtraceback: "ReprTraceback"
    reprcrash: Optional["ReprFileLocation"]

    def toterminal(self, tw: TerminalWriter) -> None:
        self.reprtraceback.toterminal(tw)
        super().toterminal(tw)


@dataclasses.dataclass(eq=False)
class ReprTraceback(TerminalRepr):
    reprentries: Sequence[Union["ReprEntry", "ReprEntryNative"]]
    extraline: Optional[str]
    style: "_TracebackStyle"

    entrysep: ClassVar = "_ "

    def toterminal(self, tw: TerminalWriter) -> None:
        # The entries might have different styles.
        for i, entry in enumerate(self.reprentries):
            if entry.style == "long":
                tw.line("")
            entry.toterminal(tw)
            if i < len(self.reprentries) - 1:
                next_entry = self.reprentries[i + 1]
                if (
                    entry.style == "long"
                    or entry.style == "short"
                    and next_entry.style == "long"
                ):
                    tw.sep(self.entrysep)

        if self.extraline:
            tw.line(self.extraline)


class ReprTracebackNative(ReprTraceback):
    def __init__(self, tblines: Sequence[str]) -> None:
        self.reprentries = [ReprEntryNative(tblines)]
        self.extraline = None
        self.style = "native"


@dataclasses.dataclass(eq=False)
class ReprEntryNative(TerminalRepr):
    lines: Sequence[str]

    style: ClassVar["_TracebackStyle"] = "native"

    def toterminal(self, tw: TerminalWriter) -> None:
        tw.write("".join(self.lines))


@dataclasses.dataclass(eq=False)
class ReprEntry(TerminalRepr):
    lines: Sequence[str]
    reprfuncargs: Optional["ReprFuncArgs"]
    reprlocals: Optional["ReprLocals"]
    reprfileloc: Optional["ReprFileLocation"]
    style: "_TracebackStyle"

    def _write_entry_lines(self, tw: TerminalWriter) -> None:
        """Write the source code portions of a list of traceback entries with syntax highlighting.

        Usually entries are lines like these:

            "     x = 1"
            ">    assert x == 2"
            "E    assert 1 == 2"

        This function takes care of rendering the "source" portions of it (the lines without
        the "E" prefix) using syntax highlighting, taking care to not highlighting the ">"
        character, as doing so might break line continuations.
        """

        if not self.lines:
            return

        # separate indents and source lines that are not failures: we want to
        # highlight the code but not the indentation, which may contain markers
        # such as ">   assert 0"
        fail_marker = f"{FormattedExcinfo.fail_marker}   "
        indent_size = len(fail_marker)
        indents: List[str] = []
        source_lines: List[str] = []
        failure_lines: List[str] = []
        for index, line in enumerate(self.lines):
            is_failure_line = line.startswith(fail_marker)
            if is_failure_line:
                # from this point on all lines are considered part of the failure
                failure_lines.extend(self.lines[index:])
                break
            else:
                if self.style == "value":
                    source_lines.append(line)
                else:
                    indents.append(line[:indent_size])
                    source_lines.append(line[indent_size:])

        tw._write_source(source_lines, indents)

        # failure lines are always completely red and bold
        for line in failure_lines:
            tw.line(line, bold=True, red=True)

    def toterminal(self, tw: TerminalWriter) -> None:
        if self.style == "short":
            assert self.reprfileloc is not None
            self.reprfileloc.toterminal(tw)
            self._write_entry_lines(tw)
            if self.reprlocals:
                self.reprlocals.toterminal(tw, indent=" " * 8)
            return

        if self.reprfuncargs:
            self.reprfuncargs.toterminal(tw)

        self._write_entry_lines(tw)

        if self.reprlocals:
            tw.line("")
            self.reprlocals.toterminal(tw)
        if self.reprfileloc:
            if self.lines:
                tw.line("")
            self.reprfileloc.toterminal(tw)

    def __str__(self) -> str:
        return "{}\n{}\n{}".format(
            "\n".join(self.lines), self.reprlocals, self.reprfileloc
        )


@dataclasses.dataclass(eq=False)
class ReprFileLocation(TerminalRepr):
    path: str
    lineno: int
    message: str

    def __post_init__(self) -> None:
        self.path = str(self.path)

    def toterminal(self, tw: TerminalWriter) -> None:
        # Filename and lineno output for each entry, using an output format
        # that most editors understand.
        msg = self.message
        i = msg.find("\n")
        if i != -1:
            msg = msg[:i]
        tw.write(self.path, bold=True, red=True)
        tw.line(f":{self.lineno}: {msg}")


@dataclasses.dataclass(eq=False)
class ReprLocals(TerminalRepr):
    lines: Sequence[str]

    def toterminal(self, tw: TerminalWriter, indent="") -> None:
        for line in self.lines:
            tw.line(indent + line)


@dataclasses.dataclass(eq=False)
class ReprFuncArgs(TerminalRepr):
    args: Sequence[Tuple[str, object]]

    def toterminal(self, tw: TerminalWriter) -> None:
        if self.args:
            linesofar = ""
            for name, value in self.args:
                ns = f"{name} = {value}"
                if len(ns) + len(linesofar) + 2 > tw.fullwidth:
                    if linesofar:
                        tw.line(linesofar)
                    linesofar = ns
                else:
                    if linesofar:
                        linesofar += ", " + ns
                    else:
                        linesofar = ns
            if linesofar:
                tw.line(linesofar)
            tw.line("")


def getfslineno(obj: object) -> Tuple[Union[str, Path], int]:
    """Return source location (path, lineno) for the given object.

    If the source cannot be determined return ("", -1).

    The line number is 0-based.
    """
    # xxx let decorators etc specify a sane ordering
    # NOTE: this used to be done in _pytest.compat.getfslineno, initially added
    #       in 6ec13a2b9.  It ("place_as") appears to be something very custom.
    obj = get_real_func(obj)
    if hasattr(obj, "place_as"):
        obj = obj.place_as  # type: ignore[attr-defined]

    try:
        code = Code.from_function(obj)
    except TypeError:
        try:
            fn = inspect.getsourcefile(obj) or inspect.getfile(obj)  # type: ignore[arg-type]
        except TypeError:
            return "", -1

        fspath = fn and absolutepath(fn) or ""
        lineno = -1
        if fspath:
            try:
                _, lineno = findsource(obj)
            except OSError:
                pass
        return fspath, lineno

    return code.path, code.firstlineno


# Relative paths that we use to filter traceback entries from appearing to the user;
# see filter_traceback.
# note: if we need to add more paths than what we have now we should probably use a list
# for better maintenance.

_PLUGGY_DIR = Path(pluggy.__file__.rstrip("oc"))
# pluggy is either a package or a single module depending on the version
if _PLUGGY_DIR.name == "__init__.py":
    _PLUGGY_DIR = _PLUGGY_DIR.parent
_PYTEST_DIR = Path(_pytest.__file__).parent


def filter_traceback(entry: TracebackEntry) -> bool:
    """Return True if a TracebackEntry instance should be included in tracebacks.

    We hide traceback entries of:

    * dynamically generated code (no code to show up for it);
    * internal traceback from pytest or its internal libraries, py and pluggy.
    """
    # entry.path might sometimes return a str object when the entry
    # points to dynamically generated code.
    # See https://bitbucket.org/pytest-dev/py/issues/71.
    raw_filename = entry.frame.code.raw.co_filename
    is_generated = "<" in raw_filename and ">" in raw_filename
    if is_generated:
        return False

    # entry.path might point to a non-existing file, in which case it will
    # also return a str object. See #1133.
    p = Path(entry.path)

    parents = p.parents
    if _PLUGGY_DIR in parents:
        return False
    if _PYTEST_DIR in parents:
        return False

    return True
