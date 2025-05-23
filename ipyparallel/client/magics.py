"""
=============
parallelmagic
=============

Magic command interface for interactive parallel work.

Usage
=====

``%autopx``

{AUTOPX_DOC}

``%px``

{PX_DOC}

``%pxresult``

{RESULT_DOC}

``%pxconfig``

{CONFIG_DOC}

"""

import inspect
import re
import sys
import time
from contextlib import nullcontext
from textwrap import dedent

from IPython.core import magic_arguments
from IPython.core.error import UsageError
from IPython.core.magic import Magics, no_var_expand

import ipyparallel as ipp

from .. import error

# -----------------------------------------------------------------------------
# Definitions of magic functions for use with IPython
# -----------------------------------------------------------------------------


def _iscoroutinefunction(f):
    """Check if a callable is a coroutine function
    (either generator-style or async def)
    """
    if inspect.isgeneratorfunction(f):
        return True
    if hasattr(inspect, 'iscoroutinefunction') and inspect.iscoroutinefunction(f):
        return True
    return False


def _asyncify(f):
    """Wrap a blocking call in a coroutine

    Does not make the call non-blocking,
    just conforms to the API assuming it is awaitable.
    For use when patching-in replacement methods.
    """

    async def async_f(*args, **kwargs):
        return f(*args, **kwargs)

    return async_f


NO_LAST_RESULT = "%pxresult recalls last %px result, which has not yet been used."


def exec_args(f):
    """decorator for adding block/targets args for execution

    applied to %pxconfig and %%px
    """
    args = [
        magic_arguments.argument(
            '-b',
            '--block',
            action="store_const",
            const=True,
            dest='block',
            help="use blocking (sync) execution",
        ),
        magic_arguments.argument(
            '-a',
            '--noblock',
            action="store_const",
            const=False,
            dest='block',
            help="use non-blocking (async) execution",
        ),
        magic_arguments.argument(
            '--stream',
            action="store_const",
            const=True,
            dest='stream',
            help="stream stdout/stderr in real-time (only valid when using blocking execution)",
        ),
        magic_arguments.argument(
            '--no-stream',
            action="store_const",
            const=False,
            dest='stream',
            help="do not stream stdout/stderr in real-time",
        ),
        magic_arguments.argument(
            '-t',
            '--targets',
            type=str,
            help="specify the targets on which to execute",
        ),
        magic_arguments.argument(
            '--verbose',
            action="store_const",
            const=True,
            dest="set_verbose",
            help="print a message at each execution",
        ),
        magic_arguments.argument(
            '--no-verbose',
            action="store_const",
            const=False,
            dest="set_verbose",
            help="don't print any messages",
        ),
        magic_arguments.argument(
            '--progress-after',
            dest="progress_after_seconds",
            type=float,
            default=None,
            help="""Wait this many seconds before showing a progress bar for task completion.

            Use -1 for no progress, 0 for always showing progress immediately.
            """,
        ),
        magic_arguments.argument(
            '--signal-on-interrupt',
            dest='signal_on_interrupt',
            type=str,
            default=None,
            help="""Send signal to engines on Keyboard Interrupt. By default a SIGINT is sent.
            Note that this is only applicable when running in blocking mode.
            Choices: SIGINT, 2, SIGKILL, 9, 0 (nop), etc.
            """,
        ),
    ]
    for a in args:
        f = a(f)
    return f


def output_args(f):
    """decorator for output-formatting args

    applied to %pxresult and %%px
    """
    args = [
        magic_arguments.argument(
            '-r',
            action="store_const",
            dest='groupby',
            const='order',
            help="collate outputs in order (same as group-outputs=order)",
        ),
        magic_arguments.argument(
            '-e',
            action="store_const",
            dest='groupby',
            const='engine',
            help="group outputs by engine (same as group-outputs=engine)",
        ),
        magic_arguments.argument(
            '--group-outputs',
            dest='groupby',
            type=str,
            choices=['engine', 'order', 'type'],
            default='type',
            help="""Group the outputs in a particular way.

            Choices are:

            **type**: group outputs of all engines by type (stdout, stderr, displaypub, etc.).
            **engine**: display all output for each engine together.
            **order**: like type, but individual displaypub output from each engine is collated.
              For example, if multiple plots are generated by each engine, the first
              figure of each engine will be displayed, then the second of each, etc.
            """,
        ),
        magic_arguments.argument(
            '-o',
            '--out',
            dest='save_name',
            type=str,
            help="""store the AsyncResult object for this computation
                 in the global namespace under this name.
            """,
        ),
    ]
    for a in args:
        f = a(f)
    return f


class ParallelMagics(Magics):
    """A set of magics useful when controlling a parallel IPython cluster."""

    # magic-related
    magics = None
    registered = True

    # suffix for magics
    suffix = ''
    # A flag showing if autopx is activated or not
    _autopx = False
    # the current view used by the magics:
    view = None
    # last result cache for %pxresult
    last_result = None
    # verbose flag
    verbose = False
    # streaming output flag
    stream_output = not ipp._NONINTERACTIVE
    # seconds to wait before showing progress bar for blocking execution
    progress_after_seconds = 2
    # signal to send to engines on keyboard-interrupt
    signal_on_interrupt = "SIGINT"

    def __init__(self, shell, view, suffix=''):
        self.view = view
        self.suffix = suffix

        # register magics
        self.magics = dict(cell={}, line={})
        line_magics = self.magics['line']

        px = 'px' + suffix
        if not suffix:
            # keep %result for legacy compatibility
            line_magics['result'] = self.result

        line_magics['pxresult' + suffix] = self.result
        line_magics[px] = self.px
        line_magics['pxconfig' + suffix] = self.pxconfig
        line_magics['auto' + px] = self.autopx

        self.magics['cell'][px] = self.cell_px

        super().__init__(shell=shell)

    def _eval_target_str(self, ts):
        if ':' in ts:
            targets = eval(f"self.view.client.ids[{ts}]")
        elif 'all' in ts:
            targets = 'all'
        else:
            targets = eval(ts)
        return targets

    def _eval_signal_str(self, sig_str: str):
        if sig_str.isdigit():
            return int(sig_str)
        return sig_str

    @magic_arguments.magic_arguments()
    @exec_args
    def pxconfig(self, line):
        """configure default targets/blocking for %px magics"""
        args = magic_arguments.parse_argstring(self.pxconfig, line)
        if args.targets:
            self.view.targets = self._eval_target_str(args.targets)
        if args.block is not None:
            self.view.block = args.block
        if args.set_verbose is not None:
            self.verbose = args.set_verbose
        if args.stream is not None:
            self.stream_output = args.stream
        if args.signal_on_interrupt is not None:
            self.signal_on_interrupt = self._eval_signal_str(args.signal_on_interrupt)

        if args.progress_after_seconds is not None:
            self.progress_after_seconds = args.progress_after_seconds

    @magic_arguments.magic_arguments()
    @output_args
    def result(self, line=''):
        """Print the result of the last asynchronous %px command.

        This lets you recall the results of %px computations after
        asynchronous submission (block=False).

        Examples
        --------
        ::

            In [23]: %px os.getpid()
            Async parallel execution on engine(s): all

            In [24]: %pxresult
            Out[8:10]: 60920
            Out[9:10]: 60921
            Out[10:10]: 60922
            Out[11:10]: 60923
        """
        args = magic_arguments.parse_argstring(self.result, line)

        if self.last_result is None:
            raise UsageError(NO_LAST_RESULT)

        if args.save_name:
            self.shell.user_ns[args.save_name] = self.last_result
            return

        self.last_result.get()
        self.last_result.display_outputs(groupby=args.groupby)

    @no_var_expand
    def px(self, line=''):
        """Executes the given python command in parallel.

        Examples
        --------
        ::

            In [24]: %px a = os.getpid()
            Parallel execution on engine(s): all

            In [25]: %px print a
            [stdout:0] 1234
            [stdout:1] 1235
            [stdout:2] 1236
            [stdout:3] 1237
        """
        return self.parallel_execute(line)

    def parallel_execute(
        self,
        cell,
        block=None,
        groupby='type',
        save_name=None,
        stream_output=None,
        progress_after=None,
        signal_on_interrupt=None,
    ):
        """implementation used by %px and %%parallel"""

        # defaults:
        block = self.view.block if block is None else block
        stream_output = self.stream_output if stream_output is None else stream_output
        signal_on_interrupt = (
            self.signal_on_interrupt
            if signal_on_interrupt is None
            else signal_on_interrupt
        )

        base = "Parallel" if block else "Async parallel"

        targets = self.view.targets
        if isinstance(targets, list) and len(targets) > 10:
            str_targets = str(targets[:4])[:-1] + ', ..., ' + str(targets[-4:])[1:]
        else:
            str_targets = str(targets)
        if self.verbose:
            print(base + f" execution on engine(s): {str_targets}")

        result = self.view.execute(cell, silent=False, block=False)
        result._fname = "%px"
        self.last_result = result

        if save_name:
            self.shell.user_ns[save_name] = result

        if block:
            try:
                if progress_after is None:
                    progress_after = self.progress_after_seconds

                cm = result.stream_output() if stream_output else nullcontext()
                with cm:
                    finished_waiting = False
                    if progress_after > 0:
                        # finite progress-after timeout
                        # wait for 'quick' results before showing progress
                        tic = time.perf_counter()
                        deadline = tic + progress_after
                        result.wait(timeout=progress_after)
                        remaining = max(deadline - time.perf_counter(), 0)
                        result.wait_for_output(timeout=remaining)
                        finished_waiting = result.done()

                    if not finished_waiting:
                        if progress_after >= 0:
                            # not an immediate result, start interactive progress
                            result.wait_interactive()
                            result.wait_for_output(1)

                    try:
                        result.get()
                    except error.CompositeError as e:
                        if stream_output and result._output_ready:
                            # already streamed, show an abbreviated result
                            raise error.AlreadyDisplayedError(e) from None
                        else:
                            raise
                # Skip redisplay if streaming output
            except KeyboardInterrupt:
                if signal_on_interrupt is not None:
                    print(
                        f"Received Keyboard Interrupt. Sending signal {signal_on_interrupt} to engines...",
                        file=sys.stderr,
                    )
                    self.view.client.send_signal(
                        signal_on_interrupt, targets=targets, block=True
                    )
                else:
                    raise
            finally:
                # always redisplay outputs if not streaming,
                # on both success and error

                if not stream_output:
                    # wait for at most 1 second for output to be complete
                    result.wait_for_output(1)
                    result.display_outputs(groupby)
        else:
            # return AsyncResult only on non-blocking submission
            return result

    @magic_arguments.magic_arguments()
    @exec_args
    @output_args
    @magic_arguments.argument(
        '--local',
        action="store_const",
        const=True,
        dest="local",
        help="also execute the cell in the local namespace",
    )
    def cell_px(self, line='', cell=None):
        """Executes the cell in parallel.

        Examples
        --------
        ::

            In [24]: %%px --noblock
               ....: a = os.getpid()
            Async parallel execution on engine(s): all

            In [25]: %%px
               ....: print a
            [stdout:0] 1234
            [stdout:1] 1235
            [stdout:2] 1236
            [stdout:3] 1237
        """

        args = magic_arguments.parse_argstring(self.cell_px, line)

        if args.targets:
            save_targets = self.view.targets
            self.view.targets = self._eval_target_str(args.targets)
        signal_on_interrupt = None
        if args.signal_on_interrupt:
            signal_on_interrupt = self._eval_signal_str(args.signal_on_interrupt)
        # if running local, don't block until after local has run
        block = False if args.local else args.block
        try:
            ar = self.parallel_execute(
                cell,
                block=block,
                groupby=args.groupby,
                save_name=args.save_name,
                stream_output=args.stream,
                progress_after=args.progress_after_seconds,
                signal_on_interrupt=signal_on_interrupt,
            )
        finally:
            if args.targets:
                self.view.targets = save_targets

        # run locally after submitting remote
        block = self.view.block if args.block is None else args.block
        if args.local:
            self.shell.run_cell(cell)
            # now apply blocking behavior to remote execution
            if block:
                ar.get()
                ar.display_outputs(args.groupby)
        if not block:
            return ar

    def autopx(self, line=''):
        """Toggles auto parallel mode.

        Once this is called, all commands typed at the command line are send to
        the engines to be executed in parallel. To control which engine are
        used, the ``targets`` attribute of the view before
        entering ``%autopx`` mode.

        Then you can do the following::

            In [25]: %autopx
            %autopx to enabled

            In [26]: a = 10
            Parallel execution on engine(s): [0,1,2,3]
            In [27]: print a
            Parallel execution on engine(s): [0,1,2,3]
            [stdout:0] 10
            [stdout:1] 10
            [stdout:2] 10
            [stdout:3] 10

            In [27]: %autopx
            %autopx disabled
        """
        if self._autopx:
            self._disable_autopx()
        else:
            self._enable_autopx()

    def _enable_autopx(self):
        """Enable %autopx mode by saving the original run_cell and installing
        pxrun_cell.
        """
        self._original_run_cell = self.shell.run_cell
        self._original_run_nodes = self.shell.run_ast_nodes

        pxrun_cell = self.pxrun_cell
        if _iscoroutinefunction(self.shell.run_cell):
            # original is a coroutine,
            # wrap ours in a coroutine
            pxrun_cell = _asyncify(pxrun_cell)
        self.shell.run_cell = pxrun_cell

        pxrun_nodes = self.pxrun_nodes
        if _iscoroutinefunction(self.shell.run_ast_nodes):
            # original is a coroutine,
            # wrap ours in a coroutine
            pxrun_nodes = _asyncify(pxrun_nodes)
        self.shell.run_ast_nodes = pxrun_nodes

        self._autopx = True
        print("%autopx enabled")

    def _disable_autopx(self):
        """Disable %autopx by restoring the original InteractiveShell.run_cell."""
        if self._autopx:
            self.shell.run_cell = self._original_run_cell
            self.shell.run_ast_nodes = self._original_run_nodes
            self._autopx = False
            print("%autopx disabled")

    def pxrun_nodes(self, *args, **kwargs):
        cell = self._px_cell
        if re.search(r'^\s*%autopx\b', cell):
            self._disable_autopx()
            return False
        else:
            try:
                self.parallel_execute(cell)
            except Exception:
                self.shell.showtraceback()
                return True
            else:
                return False

    def pxrun_cell(self, raw_cell, *args, **kwargs):
        """drop-in replacement for InteractiveShell.run_cell.

        This executes code remotely, instead of in the local namespace.

        See InteractiveShell.run_cell for details.
        """
        self._px_cell = raw_cell
        return self._original_run_cell(raw_cell, *args, **kwargs)


__doc__ = __doc__.format(
    AUTOPX_DOC=dedent(ParallelMagics.autopx.__doc__),
    PX_DOC=dedent(ParallelMagics.px.__doc__),
    RESULT_DOC=dedent(ParallelMagics.result.__doc__),
    CONFIG_DOC=dedent(ParallelMagics.pxconfig.__doc__),
)
