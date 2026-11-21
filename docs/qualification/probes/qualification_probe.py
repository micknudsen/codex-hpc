import json, os, pathlib, platform, subprocess, sys
p=pathlib.Path
cfg=json.loads(p('probe-input.json').read_text())
workspace=p(cfg['workspace'])
home=p.home()
marker='.qualification-marker-20261121'
mode=sys.argv[1]
report={'mode':mode,'cwd_matches_workspace':p.cwd()==workspace,'private_home':str(home)=='/codex-hpc-home','platform':platform.system()+' '+platform.machine(),'python':platform.python_version(),'cpus_allowed':sorted(os.sched_getaffinity(0))}
report['sentinels_inaccessible']=[]
for name in cfg['sentinels']:
    try:
        with open(name) as f:f.read(1)
        report['sentinels_inaccessible'].append(False)
    except (FileNotFoundError,PermissionError):report['sentinels_inaccessible'].append(True)
report['workspace_marker_before']=(workspace/marker).exists()
report['home_marker_before']=(home/marker).exists()
if mode=='first':
    (workspace/marker).write_text('workspace persists\n')
    (home/marker).write_text('temporary home expires\n')
report['workspace_marker_after']=(workspace/marker).exists()
report['home_marker_after']=(home/marker).exists()
# Record only resource limits, not identifying cgroup paths.
limits=[]
for line in p('/proc/self/cgroup').read_text().splitlines():
    _,controllers,rel=line.split(':',2)
    bases=[p('/sys/fs/cgroup')/rel.lstrip('/'),p('/sys/fs/cgroup')]
    if controllers:bases += [p('/sys/fs/cgroup')/controllers/rel.lstrip('/')]
    for base in bases:
        for name in ['memory.max','memory.limit_in_bytes','cpuset.cpus.effective','cpuset.cpus','cpu.max']:
            f=base/name
            try:limits.append({'file':name,'value':f.read_text().strip()})
            except (FileNotFoundError,PermissionError):pass
report['cgroup_limits']=limits
report['codex_version']=subprocess.run(['/usr/local/bin/codex','--version'],capture_output=True,text=True).stdout.strip()
mounts=p('/proc/self/mountinfo').read_text().splitlines()
report['home_mount_present']=any(line.split()[4]=='/codex-hpc-home' for line in mounts)
report['workspace_mount_present']=any(line.split()[4].replace('\\040',' ')==str(workspace) for line in mounts)
# Runtime-only record permits checking exact node-local scratch after shutdown.
roots=[line.split()[3] for line in mounts if line.split()[4]=='/codex-hpc-home']
(workspace/('scratch-'+mode+'.json')).write_text(json.dumps(roots))
(workspace/('result-'+mode+'.json')).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
assert report['cwd_matches_workspace'] and report['private_home']
assert all(report['sentinels_inaccessible'])
assert report['workspace_marker_after']
if mode=='first':assert report['home_marker_after']
else:assert report['workspace_marker_before'] and not report['home_marker_before']
