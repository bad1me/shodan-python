"""
Shodan CLI

Note: Always run "shodan init <api key>" before trying to execute any other command!

A simple interface to search Shodan, download data and parse compressed JSON files.
The following commands are currently supported:

    alert
    convert
    count
    data
    download
    honeyscore
    host
    info
    init
    myip
    parse
    radar
    scan
    search
    stats
    stream

"""

import click
import collections
import csv
import datetime
import gzip
import itertools
import os
import os.path
import shodan
import shodan.helpers as helpers
import socket
import sys
import threading
import requests
import time

# The file converters that are used to go from .json.gz to various other formats
from shodan.cli.converter import CsvConverter, KmlConverter, GeoJsonConverter, ExcelConverter, ImagesConverter

# Constants
from shodan.cli.settings import SHODAN_CONFIG_DIR, COLORIZE_FIELDS

# Helper methods
from shodan.cli.helpers import get_api_key

# Allow 3rd-parties to develop custom commands
from click_plugins import with_plugins
from pkg_resources import iter_entry_points

# Make "-h" work like "--help"
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

# Define a basestring type if necessary for Python3 compatibility
try:
    basestring
except NameError:
    basestring = str


def escape_data(args):
    # Ensure the provided string isn't unicode data
    if not isinstance(args, str):
        args = args.encode('ascii', 'replace')
    return args.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')

def timestr():
    return datetime.datetime.utcnow().strftime('%Y-%m-%d')

def open_streaming_file(directory, timestr, compresslevel=9):
    return gzip.open('%s/%s.json.gz' % (directory, timestr), 'a', compresslevel)

def get_banner_field(banner, flat_field):
    # The provided field is a collapsed form of the actual field
    fields = flat_field.split('.')

    try:
        current_obj = banner
        for field in fields:
            current_obj = current_obj[field]
        return current_obj
    except:
        pass

    return None

def match_filters(banner, filters):
    for args in filters:
        flat_field, check = args.split(':', 1)
        value = get_banner_field(banner, flat_field)

        # If the field doesn't exist on the banner then ignore the record
        if not value:
            return False

        # It must match all filters to be allowed
        field_type = type(value)

        # For lists of strings we see whether the desired value is contained in the field
        if field_type == list or isinstance(value, basestring):
            if check not in value:
                return False
        elif field_type == int:
            if int(check) != value:
                return False
        elif field_type == float:
            if float(check) != value:
                return False
        else:
            # Ignore unknown types
            pass

    return True


@with_plugins(iter_entry_points('shodan.cli.plugins'))
@click.group(context_settings=CONTEXT_SETTINGS)
def main():
    pass


@main.command()
@click.argument('input', metavar='<input file>')
@click.argument('format', metavar='<output format>', type=click.Choice(['kml', 'csv', 'geo.json', 'images', 'xlsx']))
def convert(input, format):
    """Convert the given input data file into a different format.

    Example: shodan convert data.json.gz kml
    """
    # Get the basename for the input file
    basename = input.replace('.json.gz', '').replace('.json', '')

    # Add the new file extension based on the format
    filename = '{}.{}'.format(basename, format)

    # Open the output file
    fout = open(filename, 'w')

    # Start a spinner
    finished_event = threading.Event()
    progress_bar_thread = threading.Thread(target=async_spinner, args=(finished_event,))
    progress_bar_thread.start()

    # Initialize the file converter
    converter = {
        'kml': KmlConverter,
        'csv': CsvConverter,
        'geo.json': GeoJsonConverter,
        'images': ImagesConverter,
        'xlsx': ExcelConverter,
    }.get(format)(fout)

    converter.process([input])

    finished_event.set()
    progress_bar_thread.join()

    if format == 'images':
        click.echo(click.style('\rSuccessfully extracted images to directory: {}'.format(converter.dirname), fg='green'))
    else:
        click.echo(click.style('\rSuccessfully created new file: {}'.format(filename), fg='green'))


@main.command()
@click.argument('key', metavar='<api key>')
def init(key):
    """Initialize the Shodan command-line"""
    # Create the directory if necessary
    shodan_dir = os.path.expanduser(SHODAN_CONFIG_DIR)
    if not os.path.isdir(shodan_dir):
        try:
            os.mkdir(shodan_dir)
        except OSError:
            raise click.ClickException('Unable to create directory to store the Shodan API key (%s)' % shodan_dir)

    # Make sure it's a valid API key
    key = key.strip()
    try:
        api = shodan.Shodan(key)
        test = api.info()
    except shodan.APIError as e:
        raise click.ClickException('Invalid API key')

    # Store the API key in the user's directory
    keyfile = shodan_dir + '/api_key'
    with open(keyfile, 'w') as fout:
        fout.write(key.strip())
        click.echo(click.style('Successfully initialized', fg='green'))

    os.chmod(keyfile, 0o600)


@main.group()
def alert():
    """Manage the network alerts for your account"""
    pass


@alert.command(name='clear')
def alert_clear():
    """Remove all alerts"""
    key = get_api_key()

    # Get the list
    api = shodan.Shodan(key)
    try:
        alerts = api.alerts()
        for alert in alerts:
            click.echo('Removing {} ({})'.format(alert['name'], alert['id']))
            api.delete_alert(alert['id'])
    except shodan.APIError as e:
        raise click.ClickException(e.value)
    click.echo("Alerts deleted")

@alert.command(name='create')
@click.argument('name', metavar='<name>')
@click.argument('netblock', metavar='<netblock>')
def alert_create(name, netblock):
    """Create a network alert to monitor an external network"""
    key = get_api_key()

    # Get the list
    api = shodan.Shodan(key)
    try:
        alert = api.create_alert(name, netblock)
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    click.echo(click.style('Successfully created network alert!', fg='green'))
    click.echo(click.style('Alert ID: {}'.format(alert['id']), fg='cyan'))

@alert.command(name='list')
@click.option('--expired', help='Whether or not to show expired alerts.', default=True, type=bool)
def alert_list(expired):
    """List all the active alerts"""
    key = get_api_key()

    # Get the list
    api = shodan.Shodan(key)
    try:
        results = api.alerts(include_expired=expired)
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    if len(results) > 0:
        click.echo('# {:14} {:<21} {:<15s}'.format('Alert ID', 'Name', 'IP/ Network'))
        # click.echo('#' * 65)
        for alert in results:
            click.echo(
                '{:16} {:<30} {:<35} '.format(
                    click.style(alert['id'],  fg='yellow'),
                    click.style(alert['name'], fg='cyan'),
                    click.style(', '.join(alert['filters']['ip']), fg='white')
                ),
                nl=False
            )

            if 'expired' in alert and alert['expired']:
                click.echo(click.style('expired', fg='red'))
            else:
                click.echo('')
    else:
        click.echo("You haven't created any alerts yet.")


@alert.command(name='remove')
@click.argument('alert_id', metavar='<alert ID>')
def alert_remove(alert_id):
    """Remove the specified alert"""
    key = get_api_key()

    # Get the list
    api = shodan.Shodan(key)
    try:
        results = api.delete_alert(alert_id)
    except shodan.APIError as e:
        raise click.ClickException(e.value)
    click.echo("Alert deleted")


@main.command()
@click.argument('query', metavar='<search query>', nargs=-1)
def count(query):
    """Returns the number of results for a search"""
    key = get_api_key()

    # Create the query string out of the provided tuple
    query = ' '.join(query).strip()

    # Make sure the user didn't supply an empty string
    if query == '':
        raise click.ClickException('Empty search query')

    # Perform the search
    api = shodan.Shodan(key)
    try:
        results = api.count(query)
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    click.echo(results['total'])


@main.group()
def data():
    """Bulk data access to Shodan"""
    pass


@data.command(name='list')
@click.option('--dataset', help='See the available files in the given dataset', default=None, type=str)
def data_list(dataset):
    """List available datasets or the files within those datasets."""
    # Setup the API connection
    key = get_api_key()
    api = shodan.Shodan(key)

    if dataset:
        # Show the files within this dataset
        files = api.data.list_files(dataset)

        for file in files:
            click.echo(click.style('{:20s}'.format(file['name']), fg='cyan'), nl=False)
            click.echo(click.style('{:10s}'.format(helpers.humanize_bytes(file['size'])), fg='yellow'), nl=False)
            click.echo('{}'.format(file['url']))
    else:
        # If no dataset was provided then show a list of all datasets
        datasets = api.data.list_datasets()

        for ds in datasets:
            click.echo(click.style('{:15s}'.format(ds['name']), fg='cyan'), nl=False)
            click.echo('{}'.format(ds['description']))


@data.command(name='download')
@click.option('--chunksize', help='The size of the chunks that are downloaded into memory before writing them to disk.', default=1024, type=int)
@click.option('--filename', '-O', help='Save the file as the provided filename instead of the default.')
@click.argument('dataset', metavar='<dataset>')
@click.argument('name', metavar='<file>')
def data_download(chunksize, filename, dataset, name):
    # Setup the API connection
    key = get_api_key()
    api = shodan.Shodan(key)

    # Get the file object that the user requested which will contain the URL and total file size
    file = None
    try:
        files = api.data.list_files(dataset)
        for tmp in files:
            if tmp['name'] == name:
                file = tmp
                break
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    # The file isn't available
    if not file:
        raise click.ClickException('File not found')

    # Start downloading the file
    response = requests.get(file['url'], stream=True)

    # Figure out the size of the file based on the headers
    filesize = response.headers.get('content-length', None)
    if not filesize:
        # Fall back to using the filesize provided by the API
        filesize = file['size']
    else:
        filesize = int(filesize)

    chunk_size = 1024
    limit = filesize / chunk_size

    # Create a default filename based on the dataset and the filename within that dataset
    if not filename:
        filename = '{}-{}'.format(dataset, name)

    # Open the output file and start writing to it in chunks
    with open(filename, 'wb') as fout:
        with click.progressbar(response.iter_content(chunk_size=chunk_size), length=limit) as bar:
            for chunk in bar:
                if chunk:
                    fout.write(chunk)

    click.echo(click.style('Download completed: {}'.format(filename), 'green'))


@main.command()
@click.option('--limit', help='The number of results you want to download. -1 to download all the data possible.', default=1000, type=int)
@click.argument('filename', metavar='<filename>')
@click.argument('query', metavar='<search query>', nargs=-1)
def download(limit, filename, query):
    """Download search results and save them in a compressed JSON file."""
    key = get_api_key()

    # Create the query string out of the provided tuple
    query = ' '.join(query).strip()

    # Make sure the user didn't supply an empty string
    if query == '':
        raise click.ClickException('Empty search query')

    filename = filename.strip()
    if filename == '':
        raise click.ClickException('Empty filename')

    # Add the appropriate extension if it's not there atm
    if not filename.endswith('.json.gz'):
        filename += '.json.gz'

    # Perform the search
    api = shodan.Shodan(key)

    try:
        total = api.count(query)['total']
        info = api.info()
    except:
        raise click.ClickException('The Shodan API is unresponsive at the moment, please try again later.')

    # Print some summary information about the download request
    click.echo('Search query:\t\t\t%s' % query)
    click.echo('Total number of results:\t%s' % total)
    click.echo('Query credits left:\t\t%s' % info['unlocked_left'])
    click.echo('Output file:\t\t\t%s' % filename)

    if limit > total:
        limit = total

    # A limit of -1 means that we should download all the data
    if limit <= 0:
        limit = total

    with helpers.open_file(filename, 'w') as fout:
        count = 0
        try:
            cursor = api.search_cursor(query, minify=False)
            with click.progressbar(cursor, length=limit) as bar:
                for banner in bar:
                    helpers.write_banner(fout, banner)
                    count += 1

                    if count >= limit:
                        break
        except:
            pass

        # Let the user know we're done
        if count < limit:
            click.echo(click.style('Notice: fewer results were saved than requested', 'yellow'))
        click.echo(click.style('Saved %s results into file %s' % (count, filename), 'green'))


@main.command()
@click.option('--format', help='The output format for the host information. Possible values are: pretty, csv, tsv. (placeholder)', default='pretty', type=str)
@click.option('--history', help='Show the complete history of the host.', default=False, is_flag=True)
@click.option('--filename', '-O', help='Save the host information in the given file (append if file exists).', default=None)
@click.option('--save', '-S', help='Save the host information in the a file named after the IP (append if file exists).', default=False, is_flag=True)
@click.argument('ip', metavar='<ip address>')
def host(format, history, filename, save, ip):
    """View all available information for an IP address"""
    key = get_api_key()
    api = shodan.Shodan(key)

    try:
        host = api.host(ip, history=history)

        # General info
        click.echo(click.style(ip, fg='green'))
        if len(host['hostnames']) > 0:
            click.echo('{:25s}{}'.format('Hostnames:', ';'.join(host['hostnames'])))

        if 'city' in host and host['city']:
            click.echo('{:25s}{}'.format('City:', host['city']))

        if 'country_name' in host and host['country_name']:
            click.echo('{:25s}{}'.format('Country:', host['country_name']))

        if 'os' in host and host['os']:
            click.echo('{:25s}{}'.format('Operating System:', host['os']))

        if 'org' in host and host['org']:
            click.echo('{:25s}{}'.format('Organization:', host['org']))

        if 'last_update' in host and host['last_update']:
            click.echo('{:25s}{}'.format('Updated:', host['last_update']))

        click.echo('{:25s}{}'.format('Number of open ports:', len(host['ports'])))

        # Output the vulnerabilities the host has
        if 'vulns' in host and len(host['vulns']) > 0:
            vulns = []
            for vuln in host['vulns']:
                if vuln.startswith('!'):
                    continue
                if vuln.upper() == 'CVE-2014-0160':
                    vulns.append(click.style('Heartbleed', fg='red'))
                else:
                    vulns.append(click.style(vuln, fg='red'))

            if len(vulns) > 0:
                click.echo('{:25s}'.format('Vulnerabilities:'), nl=False)

                for vuln in vulns:
                    click.echo(vuln + '\t', nl=False)

                click.echo('')

        click.echo('')

        # If the user doesn't have access to SSL/ Telnet results then we need
        # to pad the host['data'] property with empty banners so they still see
        # the port listed as open. (#63)
        if len(host['ports']) != len(host['data']):
            # Find the ports the user can't see the data for
            ports = host['ports']
            for banner in host['data']:
                if banner['port'] in ports:
                    ports.remove(banner['port'])
            
            # Add the placeholder banners
            for port in ports:
                banner = {
                    'port': port,
                    'transport': 'tcp', # All the filtered services use TCP
                    'timestamp': host['data'][-1]['timestamp'], # Use the timestamp of the oldest banner
                    'placeholder': True, # Don't store this banner when the file is saved
                }
                host['data'].append(banner)

        click.echo('Ports:')
        for banner in sorted(host['data'], key=lambda k: k['port']):
            product = ''
            version = ''
            if 'product' in banner and banner['product']:
                product = banner['product']
            if 'version' in banner and banner['version']:
                version = '({})'.format(banner['version'])

            click.echo(click.style('{:>7d}'.format(banner['port']), fg='cyan'), nl=False)
            click.echo('/', nl=False)
            click.echo(click.style('{} '.format(banner['transport']), fg='yellow'), nl=False)
            click.echo('{} {}'.format(product, version), nl=False)

            if history:
                # Format the timestamp to only show the year-month-day
                date = banner['timestamp'][:10]
                click.echo(click.style('\t\t({})'.format(date), fg='white', dim=True), nl=False)
            click.echo('')

            # Show optional ssl info
            if 'ssl' in banner:
                if 'versions' in banner['ssl'] and banner['ssl']['versions']:
                    click.echo('\t|-- SSL Versions: {}'.format(', '.join([version for version in sorted(banner['ssl']['versions']) if not version.startswith('-')])))
                if 'dhparams' in banner['ssl'] and banner['ssl']['dhparams']:
                    click.echo('\t|-- Diffie-Hellman Parameters:')
                    click.echo('\t\t{:15s}{}\n\t\t{:15s}{}'.format('Bits:', banner['ssl']['dhparams']['bits'], 'Generator:', banner['ssl']['dhparams']['generator']))
                    if 'fingerprint' in banner['ssl']['dhparams']:
                        click.echo('\t\t{:15s}{}'.format('Fingerprint:', banner['ssl']['dhparams']['fingerprint']))

        # Store the results
        if filename or save:
            if save:
                filename = '{}.json.gz'.format(ip)

            # Add the appropriate extension if it's not there atm
            if not filename.endswith('.json.gz'):
                filename += '.json.gz'

            # Create/ append to the file
            fout = helpers.open_file(filename)

            for banner in sorted(host['data'], key=lambda k: k['port']):
                if 'placeholder' not in banner:
                    helpers.write_banner(fout, banner)
    except shodan.APIError as e:
        raise click.ClickException(e.value)



@main.command()
def info():
    """Shows general information about your account"""
    key = get_api_key()
    api = shodan.Shodan(key)
    try:
        results = api.info()
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    click.echo("""Query credits available: {0}
Scan credits available: {1}
    """.format(results['query_credits'], results['scan_credits']))


@main.command()
@click.option('--color/--no-color', default=True)
@click.option('--fields', help='List of properties to output.', default='ip_str,port,hostnames,data')
@click.option('--filters', '-f', help='Filter the results for specific values using key:value pairs.', multiple=True)
@click.option('--filename', '-O', help='Save the filtered results in the given file (append if file exists).')
@click.option('--separator', help='The separator between the properties of the search results.', default='\t')
@click.argument('filenames', metavar='<filenames>', type=click.Path(exists=True), nargs=-1)
def parse(color, fields, filters, filename, separator, filenames):
    """Extract information out of compressed JSON files."""
    # Strip out any whitespace in the fields and turn them into an array
    fields = [item.strip() for item in fields.split(',')]

    if len(fields) == 0:
        raise click.ClickException('Please define at least one property to show')

    has_filters = len(filters) > 0


    # Setup the output file handle
    fout = None
    if filename:
        # If no filters were provided raise an error since it doesn't make much sense w/out them
        if not has_filters:
            raise click.ClickException('Output file specified without any filters. Need to use filters with this option.')

        # Add the appropriate extension if it's not there atm
        if not filename.endswith('.json.gz'):
            filename += '.json.gz'
        fout = helpers.open_file(filename)

    for banner in helpers.iterate_files(filenames):
        row = ''

        # Validate the banner against any provided filters
        if has_filters and not match_filters(banner, filters):
            continue

        # Append the data
        if fout:
            helpers.write_banner(fout, banner)

        # Loop over all the fields and print the banner as a row
        for field in fields:
            tmp = ''
            value = get_banner_field(banner, field)
            if value:
                field_type = type(value)

                # If the field is an array then merge it together
                if field_type == list:
                    tmp = ';'.join(value)
                elif field_type in [int, float]:
                    tmp = str(value)
                else:
                    tmp = escape_data(value)

                # Colorize certain fields if the user wants it
                if color:
                    tmp = click.style(tmp, fg=COLORIZE_FIELDS.get(field, 'white'))

                # Add the field information to the row
                row += tmp
            row += separator

        click.echo(row)


@main.command()
def myip():
    """Print your external IP address"""
    key = get_api_key()

    api = shodan.Shodan(key)
    try:
        click.echo(api.tools.myip())
    except shodan.APIError as e:
        raise click.ClickException(e.value)


@main.group()
def scan():
    """Scan an IP/ netblock using Shodan."""
    pass


@scan.command(name='internet')
@click.option('--quiet', help='Disable the printing of information to the screen.', default=False, is_flag=True)
@click.argument('port', type=int)
@click.argument('protocol', type=str)
def scan_internet(quiet, port, protocol):
    """Scan the Internet for a specific port and protocol using the Shodan infrastructure."""
    key = get_api_key()
    api = shodan.Shodan(key)

    try:
        # Submit the request to Shodan
        click.echo('Submitting Internet scan to Shodan...', nl=False)
        scan = api.scan_internet(port, protocol)
        click.echo('Done')

        # If the requested port is part of the regular Shodan crawling, then
        # we don't know when the scan is done so lets return immediately and
        # let the user decide when to stop waiting for further results.
        official_ports = api.ports()
        if port in official_ports:
            click.echo('The requested port is already indexed by Shodan. A new scan for the port has been launched, please subscribe to the real-time stream for results.')
        else:
            # Create the output file
            filename = '{0}-{1}.json.gz'.format(port, protocol)
            counter = 0
            with helpers.open_file(filename, 'w') as fout:
                click.echo('Saving results to file: {0}'.format(filename))

                # Start listening for results
                done = False

                # Keep listening for results until the scan is done
                click.echo('Waiting for data, please stand by...')
                while not done:
                    try:
                        for banner in api.stream.ports([port], timeout=90):
                            counter += 1
                            helpers.write_banner(fout, banner)

                            if not quiet:
                                click.echo('{0:<40} {1:<20} {2}'.format(
                                        click.style(helpers.get_ip(banner), fg=COLORIZE_FIELDS['ip_str']),
                                        click.style(str(banner['port']), fg=COLORIZE_FIELDS['port']),
                                        ';'.join(banner['hostnames'])
                                    )
                                )
                    except shodan.APIError as e:
                        # We stop waiting for results if the scan has been processed by the crawlers and
                        # there haven't been new results in a while
                        if done:
                            break

                        scan = api.scan_status(scan['id'])
                        if scan['status'] == 'DONE':
                            done = True
                    except socket.timeout as e:
                        # We stop waiting for results if the scan has been processed by the crawlers and
                        # there haven't been new results in a while
                        if done:
                            break

                        scan = api.scan_status(scan['id'])
                        if scan['status'] == 'DONE':
                            done = True
                    except Exception as e:
                        raise click.ClickException(repr(e))
            click.echo('Scan finished: {0} devices found'.format(counter))
    except shodan.APIError as e:
        raise click.ClickException(e.value)


@scan.command(name='protocols')
def scan_protocols():
    """List the protocols that you can scan with using Shodan."""
    key = get_api_key()
    api = shodan.Shodan(key)
    try:
        protocols = api.protocols()

        for name, description in iter(protocols.items()):
            click.echo(click.style('{0:<30}'.format(name), fg='cyan') + description)
    except shodan.APIError as e:
        raise click.ClickException(e.value)


@scan.command(name='submit')
@click.option('--wait', help='How long to wait for results to come back. If this is set to "0" or below return immediately.', default=20, type=int)
@click.option('--filename', help='Save the results in the given file.', default='', type=str)
@click.option('--force', default=False, is_flag=True)
@click.option('--verbose', default=False, is_flag=True)
@click.argument('netblocks', metavar='<ip address>', nargs=-1)
def scan_submit(wait, filename, force, verbose, netblocks):
    """Scan an IP/ netblock using Shodan."""
    key = get_api_key()
    api = shodan.Shodan(key)
    alert = None

    # Submit the IPs for scanning
    try:
        # Submit the scan
        scan = api.scan(netblocks, force=force)

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        click.echo('')
        click.echo('Starting Shodan scan at {} - {} scan credits left'.format(now, scan['credits_left']))

        if verbose:
            click.echo('# Scan ID: {}'.format(scan['id']))

        # Return immediately
        if wait <= 0:
            click.echo('Exiting now, not waiting for results. Use the API or website to retrieve the results of the scan.')
        else:
            # Setup an alert to wait for responses
            alert = api.create_alert('Scan: {}'.format(', '.join(netblocks)), netblocks)

            # Create the output file if necessary
            filename = filename.strip()
            fout = None
            if filename != '':
                # Add the appropriate extension if it's not there atm
                if not filename.endswith('.json.gz'):
                    filename += '.json.gz'
                fout = helpers.open_file(filename, 'w')

            # Start a spinner
            finished_event = threading.Event()
            progress_bar_thread = threading.Thread(target=async_spinner, args=(finished_event,))
            progress_bar_thread.start()

            # Now wait a few seconds for items to get returned
            hosts = collections.defaultdict(dict)
            done = False
            scan_start = time.time()
            cache = {}
            while not done:
                try:
                    for banner in api.stream.alert(aid=alert['id'], timeout=wait):
                        ip = banner.get('ip', banner.get('ipv6', None))
                        if not ip:
                            continue

                        # Don't show duplicate banners
                        cache_key = '{}:{}'.format(ip, banner['port'])
                        if cache_key not in cache:
                            hosts[helpers.get_ip(banner)][banner['port']] = banner
                            cache[cache_key] = True

                        # If we've grabbed data for more than 60 seconds it might just be a busy network and we should move on
                        if time.time() - scan_start >= 60:
                            scan = api.scan_status(scan['id'])

                            if verbose:
                                click.echo('# Scan status: {}'.format(scan['status']))

                            if scan['status'] == 'DONE':
                                done = True
                                break

                except shodan.APIError as e:
                    # If the connection timed out before the timeout, that means the streaming server
                    # that the user tried to reach is down. In that case, lets wait briefly and try
                    # to connect again!
                    if (time.time() - scan_start) < wait:
                        time.sleep(0.5)
                        continue

                    # Exit if the scan was flagged as done somehow
                    if done:
                        break

                    scan = api.scan_status(scan['id'])
                    if scan['status'] == 'DONE':
                        done = True

                    if verbose:
                        click.echo('# Scan status: {}'.format(scan['status']))
                except socket.timeout as e:
                    # If the connection timed out before the timeout, that means the streaming server
                    # that the user tried to reach is down. In that case, lets wait a second and try
                    # to connect again!
                    if (time.time() - scan_start) < wait:
                        continue

                    done = True
                except Exception as e:
                    finished_event.set()
                    progress_bar_thread.join()
                    raise click.ClickException(repr(e))

            finished_event.set()
            progress_bar_thread.join()

            def print_field(name, value):
                click.echo('  {:25s}{}'.format(name, value))

            def print_banner(banner):
                click.echo('    {:20s}'.format(click.style(str(banner['port']), fg='green') + '/' + banner['transport']), nl=False)

                if 'product' in banner:
                    click.echo(banner['product'], nl=False)

                    if 'version' in banner:
                        click.echo(' ({})'.format(banner['version']), nl=False)

                click.echo('')

                # Show optional ssl info
                if 'ssl' in banner:
                    if 'versions' in banner['ssl']:
                        # Only print SSL versions if they were successfully tested
                        versions = [version for version in sorted(banner['ssl']['versions']) if not version.startswith('-')]
                        if len(versions) > 0:
                            click.echo('    |-- SSL Versions: {}'.format(', '.join(versions)))
                    if 'dhparams' in banner['ssl']:
                        click.echo('    |-- Diffie-Hellman Parameters:')
                        click.echo('        {:15s}{}\n        {:15s}{}'.format('Bits:', banner['ssl']['dhparams']['bits'], 'Generator:', banner['ssl']['dhparams']['generator']))
                        if 'fingerprint' in banner['ssl']['dhparams']:
                            click.echo('        {:15s}{}'.format('Fingerprint:', banner['ssl']['dhparams']['fingerprint']))

            if hosts:
                # Remove the remaining spinner character
                click.echo('\b ')

                for ip in sorted(hosts):
                    host = next(iter(hosts[ip].items()))[1]

                    click.echo(click.style(ip, fg='cyan'), nl=False)
                    if 'hostnames' in host and host['hostnames']:
                        click.echo(' ({})'.format(', '.join(host['hostnames'])), nl=False)
                    click.echo('')

                    if 'location' in host and 'country_name' in host['location'] and host['location']['country_name']:
                        print_field('Country', host['location']['country_name'])

                        if 'city' in host['location'] and host['location']['city']:
                            print_field('City', host['location']['city'])
                    if 'org' in host and host['org']:
                        print_field('Organization', host['org'])
                    if 'os' in host and host['os']:
                        print_field('Operating System', host['os'])
                    click.echo('')

                    # Output the vulnerabilities the host has
                    if 'vulns' in host and len(host['vulns']) > 0:
                        vulns = []
                        for vuln in host['vulns']:
                            if vuln.startswith('!'):
                                continue
                            if vuln.upper() == 'CVE-2014-0160':
                                vulns.append(click.style('Heartbleed', fg='red'))
                            else:
                                vulns.append(click.style(vuln, fg='red'))

                        if len(vulns) > 0:
                            click.echo('  {:25s}'.format('Vulnerabilities:'), nl=False)

                            for vuln in vulns:
                                click.echo(vuln + '\t', nl=False)

                            click.echo('')

                    # Print all the open ports:
                    click.echo('  Open Ports:')
                    for port in sorted(hosts[ip]):
                        print_banner(hosts[ip][port])

                        # Save the banner in a file if necessary
                        if fout:
                            helpers.write_banner(fout, hosts[ip][port])

                    click.echo('')
            else:
                # Prepend a \b to remove the spinner
                click.echo('\bNo open ports found or the host has been recently crawled and cant get scanned again so soon.')
    except shodan.APIError as e:
        raise click.ClickException(e.value)
    finally:
        # Remove any alert
        if alert:
            api.delete_alert(alert['id'])


@scan.command(name='status')
@click.argument('scan_id', type=str)
def scan_status(scan_id):
    """Check the status of an on-demand scan."""
    key = get_api_key()
    api = shodan.Shodan(key)
    try:
        scan = api.scan_status(scan_id)
        click.echo(scan['status'])
    except shodan.APIError as e:
        raise click.ClickException(e.value)


@main.command()
@click.option('--color/--no-color', default=True)
@click.option('--fields', help='List of properties to show in the search results.', default='ip_str,port,hostnames,data')
@click.option('--limit', help='The number of search results that should be returned. Maximum: 1000', default=100, type=int)
@click.option('--separator', help='The separator between the properties of the search results.', default='\t')
@click.argument('query', metavar='<search query>', nargs=-1)
def search(color, fields, limit, separator, query):
    """Search the Shodan database"""
    key = get_api_key()

    # Create the query string out of the provided tuple
    query = ' '.join(query).strip()

    # Make sure the user didn't supply an empty string
    if query == '':
        raise click.ClickException('Empty search query')

    # For now we only allow up to 1000 results at a time
    if limit > 1000:
        raise click.ClickException('Too many results requested, maximum is 1,000')

    # Strip out any whitespace in the fields and turn them into an array
    fields = [item.strip() for item in fields.split(',')]

    if len(fields) == 0:
        raise click.ClickException('Please define at least one property to show')

    # Perform the search
    api = shodan.Shodan(key)
    try:
        results = api.search(query, limit=limit)
    except shodan.APIError as e:
        raise click.ClickException(e.value)
    
    # Error out if no results were found
    if results['total'] == 0:
        raise click.ClickException('No search results found')

    # We buffer the entire output so we can use click's pager functionality
    output = ''
    for banner in results['matches']:
        row = ''

        # Loop over all the fields and print the banner as a row
        for field in fields:
            tmp = ''
            if field in banner and banner[field]:
                field_type = type(banner[field])

                # If the field is an array then merge it together
                if field_type == list:
                    tmp = ';'.join(banner[field])
                elif field_type in [int, float]:
                    tmp = str(banner[field])
                else:
                    tmp = escape_data(banner[field])

                # Colorize certain fields if the user wants it
                if color:
                    tmp = click.style(tmp, fg=COLORIZE_FIELDS.get(field, 'white'))

                # Add the field information to the row
                row += tmp
            row += separator

            # click.echo(out + separator, nl=False)
        output += row + '\n'
        # click.echo('')
    click.echo_via_pager(output)


@main.command()
@click.option('--limit', help='The number of results to return.', default=10, type=int)
@click.option('--facets', help='List of facets to get statistics for.', default='country,org')
@click.option('--filename', '-O', help='Save the results in a CSV file of the provided name.', default=None)
@click.argument('query', metavar='<search query>', nargs=-1)
def stats(limit, facets, filename, query):
    """Provide summary information about a search query"""
    # Setup Shodan
    key = get_api_key()
    api = shodan.Shodan(key)

    # Create the query string out of the provided tuple
    query = ' '.join(query).strip()

    # Make sure the user didn't supply an empty string
    if query == '':
        raise click.ClickException('Empty search query')

    facets = facets.split(',')
    facets = [(facet, limit) for facet in facets]

    # Perform the search
    try:
        results = api.count(query, facets=facets)
    except shodan.APIError as e:
        raise click.ClickException(e.value)

    # Print the stats tables
    for facet in results['facets']:
        click.echo('Top {} Results for Facet: {}'.format(len(results['facets'][facet]), facet))

        for item in results['facets'][facet]:
            value = item['value']
            if isinstance(value, basestring):
                value = value.encode('ascii', errors='replace').decode('ascii')
            else:
                value = str(value)

            click.echo(click.style('{:28s}'.format(value), fg='cyan'), nl=False)
            click.echo(click.style('{:12,d}'.format(item['count']), fg='green'))

        click.echo('')

    # Create the output file if requested
    fout = None
    if filename:
        if not filename.endswith('.csv'):
            filename += '.csv'
        fout = open(filename, 'w')
        writer = csv.writer(fout, dialect=csv.excel)

        # Write the header
        writer.writerow(['Query', query])

        # Add an empty line to separate rows
        writer.writerow([])

        # Write the header that contains the facets
        row = []
        for facet in results['facets']:
            row.append(facet)
            row.append('')
        writer.writerow(row)

        # Every facet has 2 columns (key, value)
        counter = 0
        has_items = True
        while has_items:
            row = ['' for i in range(len(results['facets']) * 2)]

            pos = 0
            has_items = False
            for facet in results['facets']:
                values = results['facets'][facet]

                # Add the values for the facet into the current row
                if len(values) > counter:
                    has_items = True
                    row[pos] = values[counter]['value']
                    row[pos+1] = values[counter]['count']

                pos += 2

            # Write out the row
            if has_items:
                writer.writerow(row)

            # Move to the next row of values
            counter += 1


@main.command()
@click.option('--color/--no-color', default=True)
@click.option('--fields', help='List of properties to output.', default='ip_str,port,hostnames,data')
@click.option('--separator', help='The separator between the properties of the search results.', default='\t')
@click.option('--limit', help='The number of results you want to download. -1 to download all the data possible.', default=-1, type=int)
@click.option('--datadir', help='Save the stream data into the specified directory as .json.gz files.', default=None, type=str)
@click.option('--ports', help='A comma-separated list of ports to grab data on.', default=None, type=str)
@click.option('--quiet', help='Disable the printing of information to the screen.', is_flag=True)
@click.option('--timeout', help='Timeout. Should the shodan stream cease to send data, then timeout after <timeout> seconds.', default=0, type=int)
@click.option('--streamer', help='Specify a custom Shodan stream server to use for grabbing data.', default='https://stream.shodan.io', type=str)
@click.option('--countries', help='A comma-separated list of countries to grab data on.', default=None, type=str)
@click.option('--asn', help='A comma-separated list of ASNs to grab data on.', default=None, type=str)
@click.option('--alert', help='The network alert ID or "all" to subscribe to all network alerts on your account.', default=None, type=str)
@click.option('--compresslevel', help='The gzip compression level (0-9; 0 = no compression, 9 = most compression', default=9, type=int)
def stream(color, fields, separator, limit, datadir, ports, quiet, timeout, streamer, countries,  asn, alert, compresslevel):
    """Stream data in real-time."""
    # Setup the Shodan API
    key = get_api_key()
    api = shodan.Shodan(key)

    # Temporarily change the baseurl
    api.stream.base_url = streamer

    # Strip out any whitespace in the fields and turn them into an array
    fields = [item.strip() for item in fields.split(',')]

    if len(fields) == 0:
        raise click.ClickException('Please define at least one property to show')

    # The user must choose "ports", "countries", "asn" or nothing - can't select multiple
    # filtered streams at once.
    stream_type = []
    if ports:
        stream_type.append('ports')
    if countries:
        stream_type.append('countries')
    if asn:
        stream_type.append('asn')
    if alert:
        stream_type.append('alert')

    if len(stream_type) > 1:
        raise click.ClickException('Please use --ports, --countries OR --asn. You cant subscribe to multiple filtered streams at once.')

    stream_args = None

    # Turn the list of ports into integers
    if ports:
        try:
            stream_args = [int(item.strip()) for item in ports.split(',')]
        except:
            raise click.ClickException('Invalid list of ports')

    if alert:
        alert = alert.strip()
        if alert.lower() != 'all':
            stream_args = alert

    if asn:
        stream_args = asn.split(',')

    if countries:
        stream_args = countries.split(',')

    # Flatten the list of stream types
    # Possible values are:
    # - all
    # - asn
    # - countries
    # - ports
    if len(stream_type) == 1:
        stream_type = stream_type[0]
    else:
        stream_type = 'all'

    # Decide which stream to subscribe to based on whether or not ports were selected
    def _create_stream(name, args, timeout):
        return {
            'all': api.stream.banners(timeout=timeout),
            'alert': api.stream.alert(args, timeout=timeout),
            'asn': api.stream.asn(args, timeout=timeout),
            'countries': api.stream.countries(args, timeout=timeout),
            'ports': api.stream.ports(args, timeout=timeout),
        }.get(name, 'all')

    stream = _create_stream(stream_type, stream_args, timeout=timeout)

    counter = 0
    quit = False
    last_time = timestr()
    fout = None

    if datadir:
        fout = open_streaming_file(datadir, last_time, compresslevel)

    while not quit:
        try:
            for banner in stream:
                # Limit the number of results to output
                if limit > 0:
                    counter += 1

                    if counter > limit:
                        quit = True
                        break

                # Write the data to the file
                if datadir:
                    cur_time = timestr()
                    if cur_time != last_time:
                            last_time = cur_time
                            fout.close()
                            fout = open_streaming_file(datadir, last_time)
                    helpers.write_banner(fout, banner)

                # Print the banner information to stdout
                if not quiet:
                    row = ''

                    # Loop over all the fields and print the banner as a row
                    for field in fields:
                        tmp = ''
                        value = get_banner_field(banner, field)
                        if value:
                            field_type = type(value)

                            # If the field is an array then merge it together
                            if field_type == list:
                                tmp = ';'.join(value)
                            elif field_type in [int, float]:
                                tmp = str(value)
                            else:
                                tmp = escape_data(value)

                            # Colorize certain fields if the user wants it
                            if color:
                                tmp = click.style(tmp, fg=COLORIZE_FIELDS.get(field, 'white'))

                            # Add the field information to the row
                            row += tmp
                        row += separator

                    click.echo(row)
        except requests.exceptions.Timeout:
            raise click.ClickException('Connection timed out')
        except KeyboardInterrupt:
            quit = True
        except shodan.APIError as e:
            raise click.ClickException(e.value)
        except:
            # For other errors lets just wait a bit and try to reconnect again
            time.sleep(1)

            # Create a new stream object to subscribe to
            stream = _create_stream(stream_type, stream_args, timeout=timeout)


@main.command()
@click.argument('ip', metavar='<IP address>')
def honeyscore(ip):
    """Check whether the IP is a honeypot or not."""
    key = get_api_key()
    api = shodan.Shodan(key)

    try:
        score = api.labs.honeyscore(ip)

        if score == 1.0:
            click.echo(click.style('Honeypot detected', fg='red'))
        elif score > 0.5:
            click.echo(click.style('Probably a honeypot', fg='yellow'))
        else:
            click.echo(click.style('Not a honeypot', fg='green'))

        click.echo('Score: {}'.format(score))
    except:
        raise click.ClickException('Unable to calculate honeyscore')


@main.command()
def radar():
    """Real-Time Map of some results as Shodan finds them."""
    key = get_api_key()
    api = shodan.Shodan(key)

    from shodan.cli.worldmap import launch_map
    launch_map(api)

def async_spinner(finished):
    spinner = itertools.cycle(['-', '/', '|', '\\'])
    while not finished.is_set():
        sys.stdout.write('\b{}'.format(next(spinner)))
        sys.stdout.flush()
        finished.wait(0.2)

if __name__ == '__main__':
    main()
