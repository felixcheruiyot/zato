# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from json import dumps
import select
import sys

# sh
import sh

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from sh import RunningCommand
    from zato.common.typing_ import any_, anydict

# ################################################################################################################################
# ################################################################################################################################

def read_stdin_data(strip=True):
    """ Reads data from sys.stdin without blocking the caller - in its current form (using select),
    it will work only on Linux and OS X.
    """
    if sys.platform.startswith('win32'):
        return ''
    # Note that we check only sys.stdin for read and that there is no timeout,
    # because we expect for sys.stdin to be available immediately when we run.
    to_read, _, _ = select.select([sys.stdin], [], [], 0)

    if to_read:
        data = to_read[0].readline()
        out = data.strip() if strip else data
    else:
        out = ''

    return out

# ################################################################################################################################
# ################################################################################################################################

class CommandLineServiceInvoker:
    def __init__(
        self,
        expected_stdout=b'',  # type: bytes
        check_stdout=True,    # type: bool
        check_exit_code=True, # type: bool
        server_location=''    # type: str
        ) -> 'None':

        # Imported here to rule out circular references
        from zato.common.test.config import TestConfig

        self.check_stdout = check_stdout
        self.check_exit_code = check_exit_code

        self.expected_stdout = expected_stdout or TestConfig.default_stdout
        self.server_location = server_location or TestConfig.server_location

    def _assert_command_line_result(self, out:'RunningCommand') -> 'None':

        if self.check_exit_code:
            if out.exit_code != 0:
                raise ValueError(f'Exit code should be 0 instead `{out.exit_code}`')

        if self.check_stdout:
            if out.stdout != self.expected_stdout:
                raise ValueError(f'Stdout should {self.expected_stdout} instead of {out.stdout}')

# ################################################################################################################################

    def invoke(self, service:'str', request:'anydict') -> 'any_':
        command = sh.zato # type: ignore

        cli_params = []

        cli_params.append('service')
        cli_params.append('invoke')

        if request:
            request = dumps(request)
            cli_params.append('--payload')
            cli_params.append(request)

        cli_params.append(self.server_location)
        cli_params.append(service)

        out = command(*cli_params)
        return out

# ################################################################################################################################

    def invoke_and_test(self, service:'str') -> 'any_':
        out = self.invoke(service, {})
        self._assert_command_line_result(out)
        return out

# ################################################################################################################################
# ################################################################################################################################
