# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Django command: change region controller configuration settings."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
)

str = None

__metaclass__ = type
__all__ = []


from optparse import make_option

from django.core.management.base import BaseCommand
from maasserver.config import RegionConfiguration


class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option(
            '--dbuser', dest='dbuser', default=None,
            help="Username for the PostgreSQL account."),
        make_option(
            '--dbpassword', dest='dbpassword', default=None,
            help="Password for the PostgreSQL account."),
        make_option(
            '--dbname', dest='dbname', default=None,
            help="Name of the PostgreSQL database."),
        make_option(
            '--dbhost', dest='dbhost', default=None,
            help="Hostname of the PostgreSQL server."),
        make_option(
            '--default-url', dest='url', default=None,
            help="Full URL of this MAAS region controller"),
    )
    help = "Modify local configuration for the MAAS region controller."

    def handle(self, *args, **options):
        url = options.get('url', None)
        dbuser = options.get('dbuser', None)
        dbpassword = options.get('dbpassword', None)
        dbname = options.get('dbname', None)
        dbhost = options.get('dbhost', None)

        with RegionConfiguration.open() as config:
            if url is not None:
                config.maas_url = url
            if dbuser is not None:
                config.database_user = dbuser
            if dbpassword is not None:
                config.database_pass = dbpassword
            if dbname is not None:
                config.database_name = dbname
            if dbhost is not None:
                config.database_host = dbhost
