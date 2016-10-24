# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "AnonMachinesHandler",
    "MachineHandler",
    "MachinesHandler",
    "get_storage_layout_params",
]

import re

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
)
from formencode import validators
from formencode.validators import (
    Int,
    StringBool,
)
from maasserver import locks
from maasserver.api.interfaces import DISPLAYED_INTERFACE_FIELDS
from maasserver.api.logger import maaslog
from maasserver.api.nodes import (
    AnonNodeHandler,
    AnonNodesHandler,
    NodeHandler,
    NodesHandler,
    OwnerDataMixin,
    PowerMixin,
    PowersMixin,
)
from maasserver.api.support import (
    admin_method,
    operation,
)
from maasserver.api.utils import (
    get_mandatory_param,
    get_oauth_token,
    get_optional_list,
    get_optional_param,
)
from maasserver.enum import (
    NODE_PERMISSION,
    NODE_STATUS,
)
from maasserver.exceptions import (
    MAASAPIBadRequest,
    MAASAPIValidationError,
    NodesNotAvailable,
    NodeStateViolation,
    Unauthorized,
)
from maasserver.forms import (
    get_machine_create_form,
    get_machine_edit_form,
)
from maasserver.forms_commission import CommissionForm
from maasserver.forms_filesystem import (
    MountNonStorageFilesystemForm,
    UnmountNonStorageFilesystemForm,
)
from maasserver.models import (
    Config,
    Domain,
    Filesystem,
    Machine,
    RackController,
)
from maasserver.models.node import RELEASABLE_STATUSES
from maasserver.node_constraint_filter_forms import AcquireNodeForm
from maasserver.preseed import get_curtin_merged_config
from maasserver.storage_layouts import (
    StorageLayoutError,
    StorageLayoutForm,
    StorageLayoutMissingBootDiskError,
)
from maasserver.utils.orm import (
    get_first,
    reload_object,
)
import yaml

# Machine's fields exposed on the API.
DISPLAYED_MACHINE_FIELDS = (
    'system_id',
    'hostname',
    'domain',
    'fqdn',
    'owner',
    'owner_data',
    'boot_interface',
    'architecture',
    'min_hwe_kernel',
    'hwe_kernel',
    'cpu_count',
    'memory',
    'swap_size',
    'storage',
    'status',
    'osystem',
    'distro_series',
    'netboot',
    'power_type',
    'power_state',
    'tag_names',
    'address_ttl',
    'ip_addresses',
    ('interface_set', DISPLAYED_INTERFACE_FIELDS),
    'zone',
    'disable_ipv4',
    'constraints_by_type',
    'boot_disk',
    'blockdevice_set',
    'physicalblockdevice_set',
    'virtualblockdevice_set',
    'status_action',
    'status_message',
    'status_name',
    'node_type',
    'node_type_name',
    'special_filesystems',
)

# Limited set of machine fields exposed on the anonymous API.
DISPLAYED_ANON_MACHINE_FIELDS = (
    'system_id',
    'hostname',
    'domain',
    'fqdn',
    'architecture',
    'status',
    'power_type',
    'power_state',
    'zone',
    'status_action',
    'status_message',
    'status_name',
    'node_type',
)


def get_storage_layout_params(request, required=False, extract_params=False):
    """Return and validate the storage_layout parameter."""
    form = StorageLayoutForm(required=required, data=request.data)
    if not form.is_valid():
        raise MAASAPIValidationError(form.errors)
    # The request data needs to be mutable so replace the immutable QueryDict
    # with a mutable one.
    request.data = request.data.copy()
    storage_layout = request.data.pop('storage_layout', None)
    if not storage_layout:
        storage_layout = None
    else:
        storage_layout = storage_layout[0]
    params = {}
    # Grab all the storage layout parameters.
    if extract_params:
        for key, value in request.data.items():
            if key.startswith("storage_layout_"):
                params[key.replace("storage_layout_", "")] = value
        # Remove the storage_layout_ parameters from the request.
        for key in params:
            request.data.pop("storage_layout_%s" % key)
    return storage_layout, params


class MachineHandler(NodeHandler, OwnerDataMixin, PowerMixin):
    """Manage an individual Machine.

    The Machine is identified by its system_id.
    """
    api_doc_section_name = "Machine"

    model = Machine
    fields = DISPLAYED_MACHINE_FIELDS

    @classmethod
    def boot_interface(handler, machine):
        """The network interface which is used to boot over the network."""
        return machine.get_boot_interface()

    @admin_method
    def update(self, request, system_id):
        """Update a specific Machine.

        :param hostname: The new hostname for this machine.
        :type hostname: unicode

        :param domain: The domain for this machine. If not given the default
            domain is used.
        :type domain: unicode

        :param architecture: The new architecture for this machine.
        :type architecture: unicode

        :param min_hwe_kernel: A string containing the minimum kernel version
            allowed to be ran on this machine.
        :type min_hwe_kernel: unicode

        :param power_type: The new power type for this machine. If you use the
            default value, power_parameters will be set to the empty string.
            Available to admin users.
            See the `Power types`_ section for a list of the available power
            types.
        :type power_type: unicode

        :param power_parameters_{param1}: The new value for the 'param1'
            power parameter.  Note that this is dynamic as the available
            parameters depend on the selected value of the Machine's
            power_type.  Available to admin users. See the `Power types`_
            section for a list of the available power parameters for each
            power type.
        :type power_parameters_{param1}: unicode

        :param power_parameters_skip_check: Whether or not the new power
            parameters for this machine should be checked against the expected
            power parameters for the machine's power type ('true' or 'false').
            The default is 'false'.
        :type power_parameters_skip_check: unicode

        :param zone: Name of a valid physical zone in which to place this
            machine.
        :type zone: unicode

        :param swap_size: Specifies the size of the swap file, in bytes. Field
            accept K, M, G and T suffixes for values expressed respectively in
            kilobytes, megabytes, gigabytes and terabytes.
        :type swap_size: unicode

        :param disable_ipv4: Deprecated.  If specified, must be False.
        :type disable_ipv4: boolean

        :param cpu_count: The amount of CPU cores the machine has.
        :type cpu_count: integer

        :param memory: How much memory the machine has.
        :type memory: unicode

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to update the machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)

        Form = get_machine_edit_form(request.user)
        form = Form(data=request.data, instance=machine)

        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)

    @classmethod
    def resource_uri(cls, machine=None):
        # This method is called by piston in two different contexts:
        # - when generating an uri template to be used in the documentation
        # (in this case, it is called with node=None).
        # - when populating the 'resource_uri' field of an object
        # returned by the API (in this case, machine is a Machine object).
        machine_system_id = "system_id"
        if machine is not None:
            machine_system_id = machine.system_id
        return ('machine_handler', (machine_system_id, ))

    @operation(idempotent=False)
    def deploy(self, request, system_id):
        """Deploy an operating system to a machine.

        :param user_data: If present, this blob of user-data to be made
            available to the machines through the metadata service.
        :type user_data: base64-encoded unicode
        :param distro_series: If present, this parameter specifies the
            OS release the machine will use.
        :type distro_series: unicode
        :param hwe_kernel: If present, this parameter specified the kernel to
            be used on the machine
        :type hwe_kernel: unicode
        :param comment: Optional comment for the event log.
        :type comment: unicode

        Ideally we'd have MIME multipart and content-transfer-encoding etc.
        deal with the encapsulation of binary data, but couldn't make it work
        with the framework in reasonable time so went for a dumb, manual
        encoding instead.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to start the machine.
        Returns 503 if the start-up attempted to allocate an IP address,
        and there were no IP addresses available on the relevant cluster
        interface.
        """
        series = request.POST.get('distro_series', None)
        license_key = request.POST.get('license_key', None)
        hwe_kernel = request.POST.get('hwe_kernel', None)

        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.EDIT)

        if machine.owner is None:
            raise NodeStateViolation(
                "Can't start machine: it hasn't been allocated.")
        if not machine.distro_series and not series:
            series = Config.objects.get_config('default_distro_series')
        if None in (series, license_key, hwe_kernel):
            Form = get_machine_edit_form(request.user)
            form = Form(instance=machine)
            if series is not None:
                form.set_distro_series(series=series)
            if license_key is not None:
                form.set_license_key(license_key=license_key)
            if hwe_kernel is not None:
                form.set_hwe_kernel(hwe_kernel=hwe_kernel)
            if form.is_valid():
                form.save()
            else:
                raise MAASAPIValidationError(form.errors)
        return self.power_on(request, system_id)

    @operation(idempotent=False)
    def release(self, request, system_id):
        """Release a machine. Opposite of `Machines.allocate`.

        :param comment: Optional comment for the event log.
        :type comment: unicode
        :param erase: Erase the disk when releasing.
        :type erase: boolean
        :param secure_erase: Use the drive's secure erase feature if available.
            In some cases this can be much faster than overwriting the drive.
            Some drives implement secure erasure by overwriting themselves so
            this could still be slow.
        :type secure_erase: boolean
        :param quick_erase: Wipe 1MiB at the start and at the end of the drive
            to make data recovery inconvenient and unlikely to happen by
            accident. This is not secure.
        :type quick_erase: boolean

        If neither secure_erase nor quick_erase are specified, MAAS will
        overwrite the whole disk with null bytes. This can be very slow.

        If both secure_erase and quick_erase are specified and the drive does
        NOT have a secure erase feature, MAAS will behave as if only
        quick_erase was specified.

        If secure_erase is specified and quick_erase is NOT specified and the
        drive does NOT have a secure erase feature, MAAS will behave as if
        secure_erase was NOT specified, i.e. will overwrite the whole disk
        with null bytes. This can be very slow.

        Returns 404 if the machine is not found.
        Returns 403 if the user doesn't have permission to release the machine.
        Returns 409 if the machine is in a state where it may not be released.
        """
        comment = get_optional_param(request.POST, 'comment')
        erase = get_optional_param(
            request.POST, 'erase', default=False, validator=StringBool)
        secure_erase = get_optional_param(
            request.POST, 'secure_erase', default=None, validator=StringBool)
        quick_erase = get_optional_param(
            request.POST, 'quick_erase', default=None, validator=StringBool)
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)
        if machine.status in (NODE_STATUS.RELEASING, NODE_STATUS.READY):
            # Nothing to do if this machine is already releasing, otherwise
            # this may be a redundant retry, and the
            # postcondition is achieved, so call this success.
            pass
        elif machine.status in RELEASABLE_STATUSES:
            machine.release_or_erase(
                request.user, comment,
                erase=erase, secure_erase=secure_erase,
                quick_erase=quick_erase)
        else:
            raise NodeStateViolation(
                "Machine cannot be released in its current state ('%s')."
                % machine.display_status())
        return machine

    @operation(idempotent=False)
    def commission(self, request, system_id):
        """Begin commissioning process for a machine.

        :param enable_ssh: Whether to enable SSH for the commissioning
            environment using the user's SSH key(s).
        :type enable_ssh: bool ('0' for False, '1' for True)
        :param skip_networking: Whether to skip re-configuring the networking
            on the machine after the commissioning has completed.
        :type skip_networking: bool ('0' for False, '1' for True)
        :param skip_storage: Whether to skip re-configuring the storage
            on the machine after the commissioning has completed.
        :type skip_storage: bool ('0' for False, '1' for True)

        A machine in the 'ready', 'declared' or 'failed test' state may
        initiate a commissioning cycle where it is checked out and tested
        in preparation for transitioning to the 'ready' state. If it is
        already in the 'ready' state this is considered a re-commissioning
        process which is useful if commissioning tests were changed after
        it previously commissioned.

        Returns 404 if the machine is not found.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.ADMIN)
        form = CommissionForm(
            instance=machine, user=request.user, data=request.data)
        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)

    @operation(idempotent=False)
    def abort(self, request, system_id):
        """Abort a machine's current operation.

        :param comment: Optional comment for the event log.
        :type comment: unicode

        This currently only supports aborting of the 'Disk Erasing' operation.

        Returns 404 if the machine could not be found.
        Returns 403 if the user does not have permission to abort the
        current operation.
        """
        comment = get_optional_param(request.POST, 'comment')
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.EDIT)
        machine.abort_operation(request.user, comment)
        return machine

    @operation(idempotent=False)
    def set_storage_layout(self, request, system_id):
        """Changes the storage layout on the machine.

        This can only be preformed on an allocated machine.

        Note: This will clear the current storage layout and any extra
        configuration and replace it will the new layout.

        :param storage_layout: Storage layout for the machine. (flat, lvm,
            and bcache)

        The following are optional for all layouts:

        :param boot_size: Size of the boot partition.
        :param root_size: Size of the root partition.
        :param root_device: Physical block device to place the root partition.

        The following are optional for LVM:

        :param vg_name: Name of created volume group.
        :param lv_name: Name of created logical volume.
        :param lv_size: Size of created logical volume.

        The following are optional for Bcache:

        :param cache_device: Physical block device to use as the cache device.
        :param cache_mode: Cache mode for bcache device. (writeback,
            writethrough, writearound)
        :param cache_size: Size of the cache partition to create on the cache
            device.
        :param cache_no_part: Don't create a partition on the cache device.
            Use the entire disk as the cache device.

        Returns 400 if the machine is currently not allocated.
        Returns 404 if the machine could not be found.
        Returns 403 if the user does not have permission to set the storage
        layout.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.ADMIN)
        if machine.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Cannot change the storage layout on a machine "
                "that is not Ready.")
        storage_layout, _ = get_storage_layout_params(request, required=True)
        try:
            machine.set_storage_layout(
                storage_layout, params=request.data, allow_fallback=False)
        except StorageLayoutMissingBootDiskError:
            raise MAASAPIBadRequest(
                "Machine is missing a boot disk; no storage layout can be "
                "applied.")
        except StorageLayoutError as e:
            raise MAASAPIBadRequest(
                "Failed to configure storage layout '%s': %s" % (
                    storage_layout, str(e)))
        return machine

    @classmethod
    def special_filesystems(cls, machine):
        """Render special-purpose filesystems, like tmpfs."""
        return [
            {
                'fstype': filesystem.fstype,
                'label': filesystem.label,
                'uuid': filesystem.uuid,
                'mount_point': filesystem.mount_point,
                'mount_options': filesystem.mount_options,
            }
            for filesystem in Filesystem.objects.filter(node=machine)
        ]

    @operation(idempotent=False)
    def mount_special(self, request, system_id):
        """Mount a special-purpose filesystem, like tmpfs.

        :param fstype: The filesystem type. This must be a filesystem that
            does not require a block special device.
        :param mount_point: Path on the filesystem to mount.
        :param mount_option: Options to pass to mount(8).

        Returns 403 when the user is not permitted to mount the partition.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)
        if machine.status not in {NODE_STATUS.READY, NODE_STATUS.ALLOCATED}:
            raise NodeStateViolation(
                "Cannot mount the filesystem because the machine is not "
                "Ready or Allocated.")
        form = MountNonStorageFilesystemForm(machine, data=request.data)
        if form.is_valid():
            # Filesystem is not a first-class object in the Web API, so save
            # it but return the machine.
            form.save()
            return machine
        else:
            raise MAASAPIValidationError(form.errors)

    @operation(idempotent=False)
    def unmount_special(self, request, system_id):
        """Unmount a special-purpose filesystem, like tmpfs.

        :param mount_point: Path on the filesystem to unmount.

        Returns 403 when the user is not permitted to unmount the partition.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)
        if machine.status not in {NODE_STATUS.READY, NODE_STATUS.ALLOCATED}:
            raise NodeStateViolation(
                "Cannot unmount the filesystem because the machine is not "
                "Ready or Allocated.")
        form = UnmountNonStorageFilesystemForm(machine, data=request.data)
        if form.is_valid():
            form.save()  # Returns nothing.
            return machine
        else:
            raise MAASAPIValidationError(form.errors)

    @operation(idempotent=False)
    def clear_default_gateways(self, request, system_id):
        """Clear any set default gateways on the machine.

        This will clear both IPv4 and IPv6 gateways on the machine. This will
        transition the logic of identifing the best gateway to MAAS. This logic
        is determined based the following criteria:

        1. Managed subnets over unmanaged subnets.
        2. Bond interfaces over physical interfaces.
        3. Machine's boot interface over all other interfaces except bonds.
        4. Physical interfaces over VLAN interfaces.
        5. Sticky IP links over user reserved IP links.
        6. User reserved IP links over auto IP links.

        If the default gateways need to be specific for this machine you can
        set which interface and subnet's gateway to use when this machine is
        deployed with the `interfaces set-default-gateway` API.

        Returns 404 if the machine could not be found.
        Returns 403 if the user does not have permission to clear the default
        gateways.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.ADMIN)
        machine.gateway_link_ipv4 = None
        machine.gateway_link_ipv6 = None
        machine.save()
        return machine

    @operation(idempotent=True)
    def get_curtin_config(self, request, system_id):
        """Return the rendered curtin configuration for the machine.

        Returns 404 if the machine could not be found.
        Returns 403 if the user does not have permission to get the curtin
        configuration.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.VIEW)
        if machine.status not in [
                NODE_STATUS.DEPLOYING,
                NODE_STATUS.DEPLOYED,
                NODE_STATUS.FAILED_DEPLOYMENT]:
            raise MAASAPIBadRequest(
                "Machine %s is not in a deployment state." % machine.hostname)
        return HttpResponse(
            yaml.safe_dump(
                get_curtin_merged_config(machine), default_flow_style=False),
            content_type='text/plain')

    @operation(idempotent=False)
    def restore_networking_configuration(self, request, system_id):
        """Reset a machine's networking options to its initial state.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to reset the machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.ADMIN)
        if machine.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Machine must be in a ready state to restore networking "
                "configuration")
        machine.set_initial_networking_configuration()
        return reload_object(machine)

    @operation(idempotent=False)
    def restore_storage_configuration(self, request, system_id):
        """Reset a machine's storage options to its initial state.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to reset the machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.ADMIN)
        if machine.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Machine must be in a ready state to restore storage "
                "configuration.")
        machine.set_default_storage_layout()
        return reload_object(machine)

    @operation(idempotent=False)
    def restore_default_configuration(self, request, system_id):
        """Reset a machine's configuration to its initial state.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to reset the machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.ADMIN)
        if machine.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Machine must be in a ready state to restore default "
                "networking and storage configuration.")
        machine.set_default_storage_layout()
        machine.set_initial_networking_configuration()
        return reload_object(machine)

    @operation(idempotent=False)
    def mark_broken(self, request, system_id):
        """Mark a node as 'broken'.

        If the node is allocated, release it first.

        :param comment: Optional comment for the event log. Will be
            displayed on the node as an error description until marked fixed.
        :type comment: unicode

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to mark the node
        broken.
        """
        node = self.model.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.EDIT)
        comment = get_optional_param(request.POST, 'comment')
        if not comment:
            # read old error_description to for backward compatibility
            comment = get_optional_param(request.POST, 'error_description')
        node.mark_broken(request.user, comment)
        return node

    @operation(idempotent=False)
    def mark_fixed(self, request, system_id):
        """Mark a broken node as fixed and set its status as 'ready'.

        :param comment: Optional comment for the event log.
        :type comment: unicode

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to mark the machine
        fixed.
        """
        comment = get_optional_param(request.POST, 'comment')
        node = self.model.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.ADMIN)
        node.mark_fixed(request.user, comment)
        maaslog.info(
            "%s: User %s marked node as fixed", node.hostname,
            request.user.username)
        return node

    @operation(idempotent=False)
    def rescue_mode(self, request, system_id):
        """Begin rescue mode process for a machine.

        A machine in the 'deployed' or 'broken' state may initiate the
        rescue mode process.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to start the
        rescue mode process for this machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.ADMIN)
        machine.start_rescue_mode(request.user)
        maaslog.info(
            "%s: User %s started rescue mode.", machine.hostname,
            request.user.username)
        return machine

    @operation(idempotent=False)
    def exit_rescue_mode(self, request, system_id):
        """Exit rescue mode process for a machine.

        A machine in the 'rescue mode' state may exit the rescue mode
        process.

        Returns 404 if the machine is not found.
        Returns 403 if the user does not have permission to exit the
        rescue mode process for this machine.
        """
        machine = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.ADMIN)
        machine.stop_rescue_mode(request.user)
        maaslog.info(
            "%s: User %s stopped rescue mode.", machine.hostname,
            request.user.username)
        return machine


def create_machine(request):
    """Service an http request to create a machine.

    The machine will be in the New state.

    :param request: The http request for this machine to be created.
    :return: A `Machine`.
    :rtype: :class:`maasserver.models.Machine`.
    :raises: ValidationError
    """

    # For backwards compatibilty reasons, requests may be sent with:
    #     architecture with a '/' in it: use normally
    #     architecture without a '/' and no subarchitecture: assume 'generic'
    #     architecture without a '/' and a subarchitecture: use as specified
    #     architecture with a '/' and a subarchitecture: error
    given_arch = request.data.get('architecture', None)
    given_subarch = request.data.get('subarchitecture', None)
    given_min_hwe_kernel = request.data.get('min_hwe_kernel', None)
    altered_query_data = request.data.copy()
    if given_arch and '/' in given_arch:
        if given_subarch:
            # Architecture with a '/' and a subarchitecture: error.
            raise MAASAPIValidationError(
                'Subarchitecture cannot be specified twice.')
        # Architecture with a '/' in it: use normally.
    elif given_arch:
        if given_subarch:
            # Architecture without a '/' and a subarchitecture:
            # use as specified.
            altered_query_data['architecture'] = '/'.join(
                [given_arch, given_subarch])
            del altered_query_data['subarchitecture']
        else:
            # Architecture without a '/' and no subarchitecture:
            # assume 'generic'.
            altered_query_data['architecture'] += '/generic'

    hwe_regex = re.compile('(hwe|ga)-.+')
    has_arch_with_hwe = (
        given_arch and hwe_regex.search(given_arch) is not None)
    has_subarch_with_hwe = (
        given_subarch and hwe_regex.search(given_subarch) is not None)
    if has_arch_with_hwe or has_subarch_with_hwe:
        raise MAASAPIValidationError(
            'hwe kernel must be specified using the min_hwe_kernel argument.')

    if given_min_hwe_kernel:
        if hwe_regex.search(given_min_hwe_kernel) is None:
            raise MAASAPIValidationError(
                'min_hwe_kernel must be in the form of hwe-<LETTER>.')

    Form = get_machine_create_form(request.user)
    form = Form(data=altered_query_data, request=request)
    if form.is_valid():
        machine = form.save()
        maaslog.info("%s: Enlisted new machine", machine.hostname)
        return machine
    else:
        raise MAASAPIValidationError(form.errors)


class AnonMachineHandler(AnonNodeHandler):
    """Anonymous machine handler.

    Only outputs machine model for anonymous results.
    """
    read = create = update = delete = None
    model = Machine
    fields = DISPLAYED_ANON_MACHINE_FIELDS

    @classmethod
    def resource_uri(cls, machine):
        return ('machine_handler', (machine.system_id, ))


class AnonMachinesHandler(AnonNodesHandler):
    """Anonymous access to Machines."""
    read = update = delete = None
    base_model = Machine

    def create(self, request):
        # Note: this docstring is duplicated below. Be sure to update both.
        """Create a new Machine.

        Adding a server to a MAAS puts it on a path that will wipe its disks
        and re-install its operating system, in the event that it PXE boots.
        In anonymous enlistment (and when the enlistment is done by a
        non-admin), the machine is held in the "New" state for approval by a
        MAAS admin.

        The minimum data required is:
        architecture=<arch string> (e.g. "i386/generic")
        mac_addresses=<value> (e.g. "aa:bb:cc:dd:ee:ff")

        :param architecture: A string containing the architecture type of
            the machine. (For example, "i386", or "amd64".) To determine the
            supported architectures, use the boot-resources endpoint.
        :type architecture: unicode

        :param min_hwe_kernel: A string containing the minimum kernel version
            allowed to be ran on this machine.
        :type min_hwe_kernel: unicode

        :param subarchitecture: A string containing the subarchitecture type
            of the machine. (For example, "generic" or "hwe-t".) To determine
            the supported subarchitectures, use the boot-resources endpoint.
        :type subarchitecture: unicode

        :param mac_addresses: One or more MAC addresses for the machine. To
            specify more than one MAC address, the parameter must be specified
            twice. (such as "machines new mac_addresses=01:02:03:04:05:06
            mac_addresses=02:03:04:05:06:07")
        :type mac_addresses: unicode

        :param hostname: A hostname. If not given, one will be generated.
        :type hostname: unicode

        :param domain: The domain of the machine. If not given the default
            domain is used.
        :type domain: unicode

        :param power_type: A power management type, if applicable (e.g.
            "virsh", "ipmi").
        :type power_type:unicode

        :param power_parameters_{param}: The parameter(s) for the power_type.
            Note that this is dynamic as the available parameters depend on
            the selected value of the Machine's power_type. `Power types`_
            section for a list of the available power parameters for each
            power type.
        :type power_parameters_{param1}: unicode
        """
        return create_machine(request)

    @operation(idempotent=False)
    def accept(self, request):
        """Accept a machine's enlistment: not allowed to anonymous users.

        Always returns 401.
        """
        raise Unauthorized("You must be logged in to accept machines.")

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('machines_handler', [])


class MachinesHandler(NodesHandler, PowersMixin):
    """Manage the collection of all the machines in the MAAS."""
    api_doc_section_name = "Machines"
    anonymous = AnonMachinesHandler
    base_model = Machine
    fields = DISPLAYED_MACHINE_FIELDS

    def create(self, request):
        # Note: this docstring is duplicated above. Be sure to update both.
        """Create a new Machine.

        Adding a server to MAAS puts it on a path that will wipe its disks
        and re-install its operating system, in the event that it PXE boots.
        In anonymous enlistment (and when the enlistment is done by a
        non-admin), the machine is held in the "New" state for approval by a
        MAAS admin.

        The minimum data required is:
        architecture=<arch string> (e.g. "i386/generic")
        mac_addresses=<value> (e.g. "aa:bb:cc:dd:ee:ff")

        :param architecture: A string containing the architecture type of
            the machine. (For example, "i386", or "amd64".) To determine the
            supported architectures, use the boot-resources endpoint.
        :type architecture: unicode

        :param min_hwe_kernel: A string containing the minimum kernel version
            allowed to be ran on this machine.
        :type min_hwe_kernel: unicode

        :param subarchitecture: A string containing the subarchitecture type
            of the machine. (For example, "generic" or "hwe-t".) To determine
            the supported subarchitectures, use the boot-resources endpoint.
        :type subarchitecture: unicode

        :param mac_addresses: One or more MAC addresses for the machine. To
            specify more than one MAC address, the parameter must be specified
            twice. (such as "machines new mac_addresses=01:02:03:04:05:06
            mac_addresses=02:03:04:05:06:07")
        :type mac_addresses: unicode

        :param hostname: A hostname. If not given, one will be generated.
        :type hostname: unicode

        :param domain: The domain of the machine. If not given the default
            domain is used.
        :type domain: unicode

        :param power_type: A power management type, if applicable (e.g.
            "virsh", "ipmi").
        :type power_type: unicode
        """
        machine = create_machine(request)
        if request.user.is_superuser:
            d = machine.start_commissioning(request.user)
            # Silently ignore errors to prevent 500 errors. The commissioning
            # callbacks have their own logging. This fixes LP1600328.
            d.addErrback(lambda _: None)
        return machine

    def _check_system_ids_exist(self, system_ids):
        """Check that the requested system_ids actually exist in the DB.

        We don't check if the current user has rights to do anything with them
        yet, just that the strings are valid. If not valid raise a BadRequest
        error.
        """
        if not system_ids:
            return
        existing_machines = self.base_model.objects.filter(
            system_id__in=system_ids)
        existing_ids = set(
            existing_machines.values_list('system_id', flat=True))
        unknown_ids = system_ids - existing_ids
        if len(unknown_ids) > 0:
            raise MAASAPIBadRequest(
                "Unknown machine(s): %s." % ', '.join(unknown_ids))

    @operation(idempotent=False)
    def accept(self, request):
        """Accept declared machines into the MAAS.

        Machines can be enlisted in the MAAS anonymously or by non-admin users,
        as opposed to by an admin.  These machines are held in the New
        state; a MAAS admin must first verify the authenticity of these
        enlistments, and accept them.

        Enlistments can be accepted en masse, by passing multiple machines to
        this call.  Accepting an already accepted machine is not an error, but
        accepting one that is already allocated, broken, etc. is.

        :param machines: system_ids of the machines whose enlistment is to be
            accepted.  (An empty list is acceptable).
        :return: The system_ids of any machines that have their status changed
            by this call.  Thus, machines that were already accepted are
            excluded from the result.

        Returns 400 if any of the machines do not exist.
        Returns 403 if the user is not an admin.
        """
        system_ids = set(request.POST.getlist('machines'))
        # Check the existence of these machines first.
        self._check_system_ids_exist(system_ids)
        # Make sure that the user has the required permission.
        machines = self.base_model.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.ADMIN, ids=system_ids)
        if len(machines) < len(system_ids):
            permitted_ids = set(machine.system_id for machine in machines)
            raise PermissionDenied(
                "You don't have the required permission to accept the "
                "following machine(s): %s." % (
                    ', '.join(system_ids - permitted_ids)))
        machines = (machine.accept_enlistment(request.user)
                    for machine in machines)
        return [machine for machine in machines if machine is not None]

    @operation(idempotent=False)
    def accept_all(self, request):
        """Accept all declared machines into the MAAS.

        Machines can be enlisted in the MAAS anonymously or by non-admin users,
        as opposed to by an admin.  These machines are held in the New
        state; a MAAS admin must first verify the authenticity of these
        enlistments, and accept them.

        :return: Representations of any machines that have their status changed
            by this call.  Thus, machines that were already accepted are
            excluded from the result.
        """
        machines = self.base_model.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.ADMIN)
        machines = machines.filter(status=NODE_STATUS.NEW)
        machines = (machine.accept_enlistment(request.user)
                    for machine in machines)
        return [machine for machine in machines if machine is not None]

    @operation(idempotent=False)
    def release(self, request):
        """Release multiple machines.

        This places the machines back into the pool, ready to be reallocated.

        :param machines: system_ids of the machines which are to be released.
           (An empty list is acceptable).
        :param comment: Optional comment for the event log.
        :type comment: unicode
        :return: The system_ids of any machines that have their status
            changed by this call. Thus, machines that were already released
            are excluded from the result.

        Returns 400 if any of the machines cannot be found.
        Returns 403 if the user does not have permission to release any of
        the machines.
        Returns a 409 if any of the machines could not be released due to their
        current state.
        """
        system_ids = set(request.POST.getlist('machines'))
        comment = get_optional_param(request.POST, 'comment')
        # Check the existence of these nodes first.
        self._check_system_ids_exist(system_ids)
        # Make sure that the user has the required permission.
        machines = self.base_model.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.EDIT, ids=system_ids)
        if len(machines) < len(system_ids):
            permitted_ids = set(machine.system_id for machine in machines)
            raise PermissionDenied(
                "You don't have the required permission to release the "
                "following machine(s): %s." % (
                    ', '.join(system_ids - permitted_ids)))

        released_ids = []
        failed = []
        for machine in machines:
            if machine.status == NODE_STATUS.READY:
                # Nothing to do.
                pass
            elif machine.status in RELEASABLE_STATUSES:
                machine.release_or_erase(request.user, comment)
                released_ids.append(machine.system_id)
            else:
                failed.append(
                    "%s ('%s')"
                    % (machine.system_id, machine.display_status()))

        if any(failed):
            raise NodeStateViolation(
                "Machine(s) cannot be released in their current state: %s."
                % ', '.join(failed))
        return released_ids

    @operation(idempotent=True)
    def list_allocated(self, request):
        """Fetch Machines that were allocated to the User/oauth token."""
        token = get_oauth_token(request)
        match_ids = get_optional_list(request.GET, 'id')
        machines = Machine.objects.get_allocated_visible_machines(
            token, match_ids)
        return machines.order_by('id')

    @operation(idempotent=False)
    def allocate(self, request):
        """Allocate an available machine for deployment.

        Constraints parameters can be used to allocate a machine that possesses
        certain characteristics.  All the constraints are optional and when
        multiple constraints are provided, they are combined using 'AND'
        semantics.

        :param name: Hostname of the desired machine.
        :type name: unicode
        :param system_id: system_id of the desired machine.
        :type system_id: unicode
        :param arch: Architecture of the returned machine (e.g. 'i386/generic',
            'amd64', 'armhf/highbank', etc.).
        :type arch: unicode
        :param cpu_count: The minium number of CPUs the returned machine must
            have.
        :type cpu_count: int
        :param interfaces: A labeled constraint map associating constraint
            labels with interface properties that should be matched. Returned
            nodes must have one or more interface matching the specified
            constraints. The labeled constraint map must be in the format:
            ``<label>:<key>=<value>[,<key2>=<value2>[,...]]``

            Each key can be one of the following:

            - id: Matches an interface with the specific id
            - fabric: Matches an interface attached to the specified fabric.
            - fabric_class: Matches an interface attached to a fabric
              with the specified class.
            - ip: Matches an interface with the specified IP address
              assigned to it.
            - mode: Matches an interface with the specified mode. (Currently,
              the only supported mode is "unconfigured".)
            - name: Matches an interface with the specified name.
              (For example, "eth0".)
            - hostname: Matches an interface attached to the node with
              the specified hostname.
            - subnet: Matches an interface attached to the specified subnet.
            - space: Matches an interface attached to the specified space.
            - subnet_cidr: Matches an interface attached to the specified
              subnet CIDR. (For example, "192.168.0.0/24".)
            - type: Matches an interface of the specified type. (Valid
              types: "physical", "vlan", "bond", "bridge", or "unknown".)
            - vlan: Matches an interface on the specified VLAN.
            - vid: Matches an interface on a VLAN with the specified VID.
            - tag: Matches an interface tagged with the specified tag.
        :type interfaces: unicode
        :param mem: The minimum amount of memory (expressed in MB) the
             returned machine must have.
        :type mem: float
        :param tags: List of tags the returned machine must have.
        :type tags: list of unicodes
        :param not_tags: List of tags the acquired machine must not have.
        :type tags: List of unicodes.
        :param networks: List of networks (defined in MAAS) to which the
            machine must be attached.  A network can be identified by the name
            assigned to it in MAAS; or by an `ip:` prefix followed by any IP
            address that falls within the network; or a `vlan:` prefix
            followed by a numeric VLAN tag, e.g. `vlan:23` for VLAN number 23.
            Valid VLAN tags must be in the range of 1 to 4094 inclusive.
        :type networks: list of unicodes
        :param not_networks: List of networks (defined in MAAS) to which the
            machine must not be attached.  The returned machine won't be
            attached to any of the specified networks.  A network can be
            identified by the name assigned to it in MAAS; or by an `ip:`
            prefix followed by any IP address that falls within the network; or
            a `vlan:` prefix followed by a numeric VLAN tag, e.g. `vlan:23` for
            VLAN number 23. Valid VLAN tags must be in the range of 1 to 4094
            inclusive.
        :type not_networks: list of unicodes
        :param zone: An optional name for a physical zone the acquired
            machine should be located in.
        :type zone: unicode
        :type not_in_zone: Optional list of physical zones from which the
            machine should not be acquired.
        :type not_in_zone: List of unicodes.
        :param agent_name: An optional agent name to attach to the
            acquired machine.
        :type agent_name: unicode
        :param comment: Optional comment for the event log.
        :type comment: unicode
        :param bridge_all: Optionally create a bridge interface for every
            configured interface on the machine. The created bridges will be
            removed once the machine is released.
            (Default: False)
        :type bridge_all: boolean
        :param bridge_stp: Optionally turn spanning tree protocol on or off
            for the bridges created on every configured interface.
            (Default: off)
        :type bridge_stp: boolean
        :param bridge_fd: Optionally adjust the forward delay to time seconds.
            (Default: 15)
        :type bridge_fd: integer
        :param dry_run: Optional boolean to indicate that the machine should
            not actually be acquired (this is for support/troubleshooting, or
            users who want to see which machine would match a constraint,
            without acquiring a machine). Defaults to False.
        :type dry_run: bool
        :param verbose: Optional boolean to indicate that the user would like
            additional verbosity in the constraints_by_type field (each
            constraint will be prefixed by `verbose_`, and contain the full
            data structure that indicates which machine(s) matched).
        :type verbose: bool

        Returns 409 if a suitable machine matching the constraints could not be
        found.
        """
        form = AcquireNodeForm(data=request.data)
        comment = get_optional_param(request.POST, 'comment')
        maaslog.info(
            "Request from user %s to acquire a machine with constraints %s",
            request.user.username, request.data)
        bridge_all = get_optional_param(
            request.POST, 'bridge_all', default=False, validator=StringBool)
        bridge_stp = get_optional_param(
            request.POST, 'bridge_stp', default=False, validator=StringBool)
        bridge_fd = get_optional_param(
            request.POST, 'bridge_fd', default=False, validator=Int)
        verbose = get_optional_param(
            request.POST, 'verbose', default=False, validator=StringBool)
        dry_run = get_optional_param(
            request.POST, 'dry_run', default=False, validator=StringBool)

        if not form.is_valid():
            raise MAASAPIValidationError(form.errors)

        # This lock prevents a machine we've picked as available from
        # becoming unavailable before our transaction commits.
        with locks.node_acquire:
            machines = (
                self.base_model.objects.get_available_machines_for_acquisition(
                    request.user)
                )
            machines, storage, interfaces = form.filter_nodes(machines)
            machine = get_first(machines)
            if machine is None:
                constraints = form.describe_constraints()
                if constraints == '':
                    # No constraints. That means no machines at all were
                    # available.
                    message = "No machine available."
                else:
                    message = (
                        "No available machine matches constraints: %s"
                        % constraints)
                raise NodesNotAvailable(message)
            agent_name = request.data.get('agent_name', '')
            if not dry_run:
                machine.acquire(
                    request.user, get_oauth_token(request),
                    agent_name=agent_name, comment=comment,
                    bridge_all=bridge_all, bridge_stp=bridge_stp,
                    bridge_fd=bridge_fd)
            machine.constraint_map = storage.get(machine.id, {})
            machine.constraints_by_type = {}
            # Need to get the interface constraints map into the proper format
            # to return it here.
            # Backward compatibility: provide the storage constraints in both
            # formats.
            if len(machine.constraint_map) > 0:
                machine.constraints_by_type['storage'] = {}
                new_storage = machine.constraints_by_type['storage']
                # Convert this to the "new style" constraints map format.
                for storage_key in machine.constraint_map:
                    # Each key in the storage map is actually a value which
                    # contains the ID of the matching storage device.
                    # Convert this to a label: list-of-matches format, to
                    # match how the constraints will be done going forward.
                    new_key = machine.constraint_map[storage_key]
                    matches = new_storage.get(new_key, [])
                    matches.append(storage_key)
                    new_storage[new_key] = matches
            if len(interfaces) > 0:
                machine.constraints_by_type['interfaces'] = {
                    label: interfaces.get(label, {}).get(machine.id)
                    for label in interfaces
                }
            if verbose:
                machine.constraints_by_type['verbose_storage'] = storage
                machine.constraints_by_type['verbose_interfaces'] = interfaces
            return machine

    @admin_method
    @operation(idempotent=False)
    def add_chassis(self, request):
        """Add special hardware types.

        :param chassis_type: The type of hardware.
            mscm is the type for the Moonshot Chassis Manager.
            msftocs is the type for the Microsoft OCS Chassis Manager.
            powerkvm is the type for Virtual Machines on Power KVM,
            managed by Virsh.
            seamicro15k is the type for the Seamicro 1500 Chassis.
            ucsm is the type for the Cisco UCS Manager.
            virsh is the type for virtual machines managed by Virsh.
            vmware is the type for virtual machines managed by VMware.
        :type chassis_type: unicode

        :param hostname: The URL, hostname, or IP address to access the
            chassis.
        :type url: unicode

        :param username: The username used to access the chassis. This field
            is required for the seamicro15k, vmware, mscm, msftocs, and ucsm
            chassis types.
        :type username: unicode

        :param password: The password used to access the chassis. This field
            is required for the seamicro15k, vmware, mscm, msftocs, and ucsm
            chassis types.
        :type password: unicode

        :param accept_all: If true, all enlisted machines will be
            commissioned.
        :type accept_all: unicode

        :param rack_controller: The system_id of the rack controller to send
            the add chassis command through. If none is specifed MAAS will
            automatically determine the rack controller to use.
        :type rack_controller: unicode

        :param domain: The domain that each new machine added should use.
        :type domain: unicode

        The following are optional if you are adding a virsh, vmware, or
        powerkvm chassis:

        :param prefix_filter: Filter machines with supplied prefix.
        :type prefix_filter: unicode

        The following are optional if you are adding a seamicro15k chassis:

        :param power_control: The power_control to use, either ipmi (default),
            restapi, or restapi2.
        :type power_control: unicode

        The following are optional if you are adding a vmware or msftocs
        chassis.

        :param port: The port to use when accessing the chassis.
        :type port: integer

        The following are optioanl if you are adding a vmware chassis:

        :param protocol: The protocol to use when accessing the VMware
            chassis (default: https).
        :type protocol: unicode

        :return: A string containing the chassis powered on by which rack
            controller.

        Returns 404 if no rack controller can be found which has access to the
        given URL.
        Returns 403 if the user does not have access to the rack controller.
        Returns 400 if the required parameters were not passed.
        """
        chassis_type = get_mandatory_param(
            request.POST, 'chassis_type',
            validator=validators.OneOf([
                'mscm', 'msftocs', 'powerkvm', 'seamicro15k', 'ucsm', 'virsh',
                'vmware']))
        hostname = get_mandatory_param(request.POST, 'hostname')

        if chassis_type in (
                'mscm', 'msftocs', 'seamicro15k', 'ucsm', 'vmware'):
            username = get_mandatory_param(request.POST, 'username')
            password = get_mandatory_param(request.POST, 'password')
        else:
            username = get_optional_param(request.POST, 'username')
            password = get_optional_param(request.POST, 'password')
            if username is not None and chassis_type in ('powerkvm', 'virsh'):
                return HttpResponseBadRequest(
                    "username can not be specified when using the %s chassis."
                    % chassis_type, content_type=(
                        "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        accept_all = get_optional_param(request.POST, 'accept_all')
        if isinstance(accept_all, str):
            accept_all = accept_all.lower() == 'true'
        else:
            accept_all = False

        # Only available with virsh, vmware, and powerkvm
        prefix_filter = get_optional_param(request.POST, 'prefix_filter')
        if (prefix_filter is not None and
                chassis_type not in ('powerkvm', 'virsh', 'vmware')):
            return HttpResponseBadRequest(
                "prefix_filter is unavailable with the %s chassis type" %
                chassis_type, content_type=(
                    "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        # Only available with seamicro15k
        power_control = get_optional_param(
            request.POST, 'power_control',
            validator=validators.OneOf(['ipmi', 'restapi', 'restapi2']))
        if power_control is not None and chassis_type != 'seamicro15k':
            return HttpResponseBadRequest(
                "power_control is unavailable with the %s chassis type" %
                chassis_type, content_type=(
                    "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        # Only available with vmware or msftocs
        port = get_optional_param(request.POST, 'port')
        if port is not None and chassis_type not in ('msftocs', 'vmware'):
            return HttpResponseBadRequest(
                "port is unavailable with the %s chassis type" %
                chassis_type, content_type=(
                    "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        # Only available with vmware
        protocol = get_optional_param(request.POST, 'protocol')
        if protocol is not None and chassis_type != 'vmware':
            return HttpResponseBadRequest(
                "protocol is unavailable with the %s chassis type" %
                chassis_type, content_type=(
                    "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        # If given a domain make sure it exists first
        domain_name = get_optional_param(request.POST, 'domain')
        if domain_name is not None:
            try:
                domain = Domain.objects.get(id=int(domain_name))
            except ValueError:
                try:
                    domain = Domain.objects.get(name=domain_name)
                except Domain.DoesNotExist:
                    return HttpResponseNotFound(
                        "Unable to find specified domain %s" % domain_name)
            domain_name = domain.name

        rack_controller = get_optional_param(request.POST, 'rack_controller')
        if rack_controller is None:
            rack = RackController.objects.get_accessible_by_url(hostname)
            if not rack:
                return HttpResponseNotFound(
                    "Unable to find a rack controller with access to chassis "
                    "%s" % hostname, content_type=(
                        "text/plain; charset=%s" % settings.DEFAULT_CHARSET))
        else:
            try:
                rack = RackController.objects.get(
                    Q(system_id=rack_controller) | Q(hostname=rack_controller))
            except RackController.DoesNotExist:
                return HttpResponseNotFound(
                    "Unable to find specified rack %s" % rack_controller,
                    content_type=(
                        "text/plain; charset=%s" % settings.DEFAULT_CHARSET))

        rack.add_chassis(
            request.user.username, chassis_type, hostname, username, password,
            accept_all, domain_name, prefix_filter, power_control, port,
            protocol)

        return HttpResponse(
            "Asking %s to add machines from chassis %s" % (
                rack.hostname, hostname),
            content_type=("text/plain; charset=%s" % settings.DEFAULT_CHARSET))

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('machines_handler', [])
