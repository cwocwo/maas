# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Django command: configure the authentication source."""

__all__ = []

import argparse
import json

from django.core.exceptions import ValidationError
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.core.validators import URLValidator
from django.db import DEFAULT_DB_ALIAS
from maasserver.management.commands.createadmin import read_input
from maasserver.models import Config


class InvalidURLError(CommandError):
    """User did not provide a valid URL."""


def prompt_for_external_auth_url(existing_url):
    if existing_url == '':
        existing_url = 'none'
    new_url = read_input(
        "URL to external IDM server [default={}]: ".format(existing_url))
    if new_url == '':
        new_url = existing_url
    return new_url


def read_agent_file(agent_file):
    with open(agent_file) as fh:
        details = json.load(fh)
    try:
        agent_details = details.get('agents', []).pop(0)
    except IndexError:
        raise ValueError('No agent users found')
    auth_url = agent_details.get('url')
    auth_user = agent_details.get('username')
    auth_key = details.get('key', {}).get('private')
    return auth_url, auth_user, auth_key


valid_url = URLValidator(schemes=('http', 'https'))


def is_valid_auth_url(auth_url):
    try:
        valid_url(auth_url)
    except ValidationError:
        return False
    return True


def config_auth(config_manager, auth_url, auth_user, auth_key):
        config_manager.set_config('external_auth_url', auth_url)
        config_manager.set_config('external_auth_user', auth_user)
        config_manager.set_config('external_auth_key', auth_key)


class Command(BaseCommand):
    help = "Configure external authentication."

    def add_arguments(self, parser):
        parser.add_argument(
            '--idm-url', default=None,
            help=(
                "The URL to the external IDM server to use for "
                "authentication. Specify '' or 'none' to unset it."))
        parser.add_argument(
            '--idm-user', default=None,
            help="The username to access the IDM server API.")
        parser.add_argument(
            '--idm-key', default=None,
            help="The private key to access the IDM server API.")
        parser.add_argument(
            '--idm-agent-file', type=argparse.FileType('r'),
            help="Agent file containing IDM authentication information")

    def handle(self, *args, **options):
        config_manager = Config.objects.db_manager(DEFAULT_DB_ALIAS)

        auth_url, auth_user, auth_key = None, '', ''

        agent_file = options.get('idm_agent_file')
        if agent_file:
            auth_url, auth_user, auth_key = read_agent_file(agent_file)
            config_auth(config_manager, auth_url, auth_user, auth_key)
            return

        auth_url = options.get('idm_url')
        if auth_url is None:
            existing_url = config_manager.get_config('external_auth_url')
            auth_url = prompt_for_external_auth_url(existing_url)
        if auth_url == 'none':
            auth_url = ''
        if auth_url:
            if not is_valid_auth_url(auth_url):
                raise InvalidURLError(
                    "Please enter a valid http or https URL.")
            auth_user = options.get('idm_user')
            auth_key = options.get('idm_key')
            if not auth_user:
                auth_user = read_input("Username for IDM API access: ")
            if not auth_key:
                auth_key = read_input("Private key for IDM API access: ")

        config_auth(config_manager, auth_url, auth_user, auth_key)
