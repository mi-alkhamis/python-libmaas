# Copyright 2012-2015 Canonical Ltd. Copyright 2015 Alburnum Ltd.
# This software is licensed under the GNU Affero General Public
# License version 3 (see LICENSE).

"""Commands for interacting with a remote MAAS."""

__all__ = []

from abc import (
    ABCMeta,
    abstractmethod,
)
import argparse
from itertools import chain
from operator import itemgetter
import sys
from textwrap import fill

from alburnum.maas import (
    bones,
    utils,
    viscera,
)
from alburnum.maas.auth import obtain_credentials
from alburnum.maas.bones import CallError
from alburnum.maas.creds import Credentials
import argcomplete
import colorclass
import terminaltables


def check_valid_apikey(_1, _2, _3):  # TODO
    return True


def colorized(text):
    if sys.stdout.isatty():
        # Don't return value_colors; returning the Color instance allows
        # terminaltables to correctly calculate alignment and padding.
        return colorclass.Color(text)
    else:
        return colorclass.Color(text).value_no_colors


def prep(cell):
    if cell is None:
        return ""
    elif isinstance(cell, str):
        return cell
    else:
        return str(cell)


def prep_table(data, prep=prep):
    """Ensure that `data` is a list of lists of strings.

    The `terminaltables` library strongly insists on `list`s of `list`s; even
    tuples are rejected. It also does not grok `None` or any other non-string
    as column values.
    """
    return [
        [prep(col) for col in row]
        for row in data
    ]


def make_table(data):
    data = prep_table(data)
    if sys.stdout.isatty():
        return terminaltables.SingleTable(data)
    else:
        return terminaltables.AsciiTable(data)


def print_table(data):
    print(make_table(data).table)


class ArgumentParser(argparse.ArgumentParser):
    """Specialisation of argparse's parser with better support for subparsers.

    Specifically, the one-shot `add_subparsers` call is disabled, replaced by
    a lazily evaluated `subparsers` property.
    """

    def __init__(self, *args, **kwargs):
        if "formatter_class" not in kwargs:
            kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
        super(ArgumentParser, self).__init__(*args, **kwargs)

    def add_subparsers(self):
        raise NotImplementedError(
            "add_subparsers has been disabled")

    @property
    def subparsers(self):
        try:
            return self.__subparsers
        except AttributeError:
            parent = super(ArgumentParser, self)
            self.__subparsers = parent.add_subparsers(title="drill down")
            self.__subparsers.metavar = "COMMAND"
            return self.__subparsers

    def error(self, message):
        """Make the default error messages more helpful

        Override default ArgumentParser error method to print the help menu
        generated by ArgumentParser instead of just printing out a list of
        valid arguments.
        """
        self.print_help(sys.stderr)
        print(file=sys.stderr)
        print(message, file=sys.stderr)
        raise SystemExit(2)


class Command(metaclass=ABCMeta):
    """A base class for composing commands.

    This adheres to the expectations of `register`.
    """

    def __init__(self, parser):
        super(Command, self).__init__()
        self.parser = parser

    @abstractmethod
    def __call__(self, options):
        """Execute this command."""

    @classmethod
    def name(cls):
        """Return the preferred name as which this command will be known."""
        name = cls.__name__.replace("_", "-").lower()
        name = name[4:] if name.startswith("cmd-") else name
        return name

    @classmethod
    def register(cls, parser, name=None):
        """Register this command as a sub-parser of `parser`.

        :type parser: An instance of `ArgumentParser`.
        """
        help_title, help_body = utils.parse_docstring(cls)
        command_parser = parser.subparsers.add_parser(
            cls.name() if name is None else name, help=help_title,
            description=help_title, epilog=help_body)
        command_parser.set_defaults(execute=cls(command_parser))


class cmd_login(Command):
    """Log in to a remote API, and remember its description and credentials.

    If credentials are not provided on the command-line, they will be prompted
    for interactively.
    """

    def __init__(self, parser):
        super(cmd_login, self).__init__(parser)
        parser.add_argument(
            "profile_name", metavar="profile-name", help=(
                "The name with which you will later refer to this remote "
                "server and credentials within this tool."
                ))
        parser.add_argument(
            "url", type=utils.api_url, help=(
                "The URL of the remote API, e.g. http://example.com/MAAS/ "
                "or http://example.com/MAAS/api/1.0/ if you wish to specify "
                "the API version."))
        parser.add_argument(
            "credentials", nargs="?", default=None, help=(
                "The credentials, also known as the API key, for the "
                "remote MAAS server. These can be found in the user "
                "preferences page in the web UI; they take the form of "
                "a long random-looking string composed of three parts, "
                "separated by colons."
                ))
        parser.add_argument(
            '-k', '--insecure', action='store_true', help=(
                "Disable SSL certificate check"), default=False)
        parser.set_defaults(credentials=None)

    def __call__(self, options):
        # Try and obtain credentials interactively if they're not given, or
        # read them from stdin if they're specified as "-".
        credentials = obtain_credentials(options.credentials)
        # Check for bogus credentials. Do this early so that the user is not
        # surprised when next invoking the MAAS CLI.
        if credentials is not None:
            try:
                valid_apikey = check_valid_apikey(
                    options.url, credentials, options.insecure)
            except CallError as e:
                raise SystemExit("%s" % e)
            else:
                if not valid_apikey:
                    raise SystemExit("The MAAS server rejected your API key.")
        # Establish a session with the remote API.
        session = bones.SessionAPI.fromURL(
            options.url, credentials=credentials, insecure=options.insecure)
        # Save the config.
        profile_name = options.profile_name
        with utils.ProfileConfig.open() as config:
            config[profile_name] = {
                "credentials": credentials,
                "description": session.description,
                "name": profile_name,
                "url": options.url,
                }
            profile = config[profile_name]
        self.print_whats_next(profile)

    @staticmethod
    def print_whats_next(profile):
        """Explain what to do next."""
        what_next = [
            "You are now logged in to the MAAS server at {url} "
            "with the profile name '{name}'.",
            "For help with the available commands, try:",
            "  maas --help",
            ]
        print()
        for message in what_next:
            message = message.format(**profile)
            print(fill(message))
            print()


class cmd_logout(Command):
    """Log out of a remote API, purging any stored credentials.

    This will remove the given profile from your command-line  client.  You
    can re-create it by logging in again later.
    """

    def __init__(self, parser):
        super(cmd_logout, self).__init__(parser)
        parser.add_argument(
            "profile_name", metavar="profile-name", help=(
                "The name with which a remote server and its credentials "
                "are referred to within this tool."
                ))

    def __call__(self, options):
        with utils.ProfileConfig.open() as config:
            del config[options.profile_name]


class cmd_list_profiles(Command):
    """List remote APIs that have been logged-in to."""

    def __call__(self, options):
        rows = [["Profile name", "URL"]]

        with utils.ProfileConfig.open() as config:
            for profile_name in sorted(config):
                profile = config[profile_name]
                url, creds = profile["url"], profile["credentials"]
                if creds is None:
                    rows.append([profile_name, url, "(anonymous)"])
                else:
                    rows.append([profile_name, url])

        print_table(rows)


class cmd_refresh_profiles(Command):
    """Refresh the API descriptions of all profiles.

    This retrieves the latest version of the help information for each
    profile.  Use it to update your command-line client's information after
    an upgrade to the MAAS server.
    """

    def __call__(self, options):
        with utils.ProfileConfig.open() as config:
            for profile_name in config:
                profile = config[profile_name]
                url, creds = profile["url"], profile["credentials"]
                session = bones.SessionAPI.fromURL(
                    url, credentials=Credentials(*creds))
                profile["description"] = session.description
                config[profile_name] = profile


class OriginCommand(Command):

    def __init__(self, parser):
        super(OriginCommand, self).__init__(parser)
        parser.add_argument("profile_name", metavar="profile-name")

    def __call__(self, options):
        session = bones.SessionAPI.fromProfileName(options.profile_name)
        origin = viscera.Origin(session)
        return self.execute(origin, options)

    def execute(self, options, origin):
        raise NotImplementedError()


class cmd_list_nodes(OriginCommand):
    """List nodes."""

    status_colours = {
        # "New": "",  # White.
        "Commissioning": "autoyellow",
        "Failed commissioning": "autored",
        "Missing": "red",
        "Ready": "autogreen",
        "Reserved": "autoblue",
        "Allocated": "autoblue",
        "Deploying": "autoblue",
        "Deployed": "autoblue",
        # "Retired": "",  # White.
        "Broken": "autored",
        "Failed deployment": "autored",
        "Releasing": "autoblue",
        "Releasing failed": "autored",
        "Disk erasing": "autoblue",
        "Failed disk erasing": "autored",
    }

    def colourised_status(self, node):
        status = node.substatus_name
        if status in self.status_colours:
            colour = self.status_colours[status]
            return colorized("{%s}%s{/%s}" % (colour, status, colour))
        else:
            return status

    power_colours = {
        "on": "autogreen",
        # "off": "",  # White.
        "error": "autored",
    }

    def colourised_power(self, node):
        state = node.power_state
        if state in self.power_colours:
            colour = self.power_colours[state]
            return colorized("{%s}%s{/%s}" % (
                colour, state.capitalize(), colour))
        else:
            return state.capitalize()

    def execute(self, origin, options):
        head = [
            ["Hostname", "System ID", "Arch", "#CPUs",
             "RAM", "Status", "Power"],
        ]
        body = (
            (node.hostname, node.system_id, node.architecture,
             node.cpus, node.memory, self.colourised_status(node),
             self.colourised_power(node))
            for node in origin.Nodes
        )
        body = sorted(body, key=itemgetter(0))
        print_table(chain(head, body))


class cmd_list_tags(OriginCommand):
    """List tags."""

    def execute(self, origin, options):
        head = [["Tag name", "Definition", "Kernel options", "Comment"]]
        body = (
            (tag.name, tag.definition, tag.kernel_opts, tag.comment)
            for tag in origin.Tags
        )
        body = sorted(body, key=itemgetter(0))
        print_table(chain(head, body))


class cmd_list_files(OriginCommand):
    """List files."""

    def execute(self, origin, options):
        head = [["File name"]]
        body = ((file.filename,) for file in origin.Files)
        body = sorted(body, key=itemgetter(0))
        print_table(chain(head, body))


class cmd_list_users(OriginCommand):
    """List users."""

    def execute(self, origin, options):
        head = [["User name", "Email address", "Admin?"]]
        body = (
            (user.username, user.email, colorized(
                "{autogreen}Yes{/autogreen}" if user.is_admin else "No"))
            for user in origin.Users
        )
        body = sorted(body, key=itemgetter(0))
        print_table(chain(head, body))


class cmd_list_xxx(OriginCommand):
    """List xxxs."""

    def execute(self, origin, options):
        xxxs = list(origin.Xxxs)
        first = xxxs[0]  # Making an assumption...
        fields = sorted(first._data)
        head = [fields]
        body = (
            (xxx._data.get(field) for field in fields)
            for xxx in xxxs
        )
        print_table(chain(head, body))


def prepare_parser(argv):
    """Create and populate an argument parser."""
    parser = ArgumentParser(
        description="Interact with a remote MAAS server.", prog=argv[0],
        epilog="http://maas.ubuntu.com/")

    # Basic commands.
    cmd_login.register(parser)
    cmd_logout.register(parser)
    cmd_list_profiles.register(parser)
    cmd_refresh_profiles.register(parser)

    # Other commands.
    cmd_list_nodes.register(parser)
    cmd_list_tags.register(parser)
    cmd_list_files.register(parser)
    cmd_list_users.register(parser)

    parser.add_argument(
        '--debug', action='store_true', default=False,
        help=argparse.SUPPRESS)

    return parser


def main(argv=sys.argv):
    parser = prepare_parser(argv)
    argcomplete.autocomplete(parser)

    try:
        options = parser.parse_args(argv[1:])
        try:
            execute = options.execute
        except AttributeError:
            parser.error("No arguments given.")
        else:
            execute(options)
    except KeyboardInterrupt:
        raise SystemExit(1)
    except Exception as error:
        if options.debug:
            raise
        else:
            # Note: this will call sys.exit() when finished.
            parser.error("%s" % error)
