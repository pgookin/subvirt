%{!?_unitdir:%global _unitdir /usr/lib/systemd/system}
%{!?_tmpfilesdir:%global _tmpfilesdir /usr/lib/tmpfiles.d}
%{!?systemd_post:%global systemd_post() %{nil}}
%{!?systemd_preun:%global systemd_preun() %{nil}}
%{!?systemd_postun_with_restart:%global systemd_postun_with_restart() %{nil}}

Name:           truenas-libvirt-provider
Version:        0.1.0
Release:        10%{?dist}
Summary:        TrueNAS provider daemon for libvirt storage pools
License:        LGPL-2.1-or-later
BuildArch:      noarch
Requires:       python3
Requires:       iscsi-initiator-utils
Requires:       nvme-cli
Requires:       kmod
Requires:       systemd

%description
Provider daemon used by the local libvirt TrueNAS storage backend to create,
export, discover, and resolve TrueNAS-backed iSCSI and NVMe-oF zvol volumes.

%prep

%build

%install
install -d -m 0755 %{buildroot}%{_libexecdir}/truenas-libvirt
install -m 0755 %{_sourcedir}/truenas_provider.py %{buildroot}%{_libexecdir}/truenas-libvirt/truenas_provider.py
install -m 0755 %{_sourcedir}/truenas_provider_daemon.py %{buildroot}%{_libexecdir}/truenas-libvirt/truenas_provider_daemon.py
install -d -m 0750 %{buildroot}%{_sysconfdir}/truenas-libvirt
install -m 0640 %{_sourcedir}/config.example.json %{buildroot}%{_sysconfdir}/truenas-libvirt/config.json
install -d -m 0755 %{buildroot}%{_unitdir}
install -m 0644 %{_sourcedir}/truenas-libvirt-provider.service %{buildroot}%{_unitdir}/truenas-libvirt-provider.service
install -d -m 0755 %{buildroot}%{_tmpfilesdir}
install -m 0644 %{_sourcedir}/truenas-libvirt-provider.conf %{buildroot}%{_tmpfilesdir}/truenas-libvirt-provider.conf

%post
%systemd_post truenas-libvirt-provider.service

%preun
%systemd_preun truenas-libvirt-provider.service

%postun
%systemd_postun_with_restart truenas-libvirt-provider.service

%files
%dir %attr(0750, root, root) %{_sysconfdir}/truenas-libvirt
%config(noreplace) %attr(0640, root, root) %{_sysconfdir}/truenas-libvirt/config.json
%dir %{_libexecdir}/truenas-libvirt
%attr(0755, root, root) %{_libexecdir}/truenas-libvirt/truenas_provider.py
%attr(0755, root, root) %{_libexecdir}/truenas-libvirt/truenas_provider_daemon.py
%{_unitdir}/truenas-libvirt-provider.service
%{_tmpfilesdir}/truenas-libvirt-provider.conf

%changelog
* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-10
- Require local transport readiness before creating or exporting volumes.
- Start iscsid with the provider service.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-7
- Keep pool refresh working when stale exports lack local by-id paths.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-6
- Reuse iSCSI initiator groups that already contain the current host.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-5
- Add current host access when reusing iSCSI targets.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-4
- Reuse existing NVMe-oF namespaces by zvol path.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-3
- Reuse existing iSCSI targets by alias.

* Mon Jun 15 2026 subvirt local build <root@localhost> - 0.1.0-2
- Sanitize managed export names for TrueNAS limits.

* Sun Jun 14 2026 subvirt local build <root@localhost> - 0.1.0-1
- Initial local package.
