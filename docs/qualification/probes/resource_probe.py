import json,pathlib,os
p=pathlib.Path
rows=[]
for line in p('/proc/self/cgroup').read_text().splitlines():
    _,controllers,rel=line.split(':',2)
    if controllers:continue
    base=p('/sys/fs/cgroup')
    current=base/rel.lstrip('/')
    depth=0
    while current==base or base in current.parents:
        row={'ancestor_depth':depth}
        for name in ['memory.max','memory.high','cpuset.cpus.effective']:
            try:row[name]=(current/name).read_text().strip()
            except (FileNotFoundError,PermissionError):pass
        rows.append(row)
        if current==base:break
        current=current.parent;depth+=1
finite=[int(r['memory.max']) for r in rows if r.get('memory.max','max').isdigit()]
report={'cpu_affinity_count':len(os.sched_getaffinity(0)),'ancestor_limits':rows,'effective_memory_limit':min(finite) if finite else None}
p('resource-result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
assert report['cpu_affinity_count']==2
assert report['effective_memory_limit']==4*1024**3
