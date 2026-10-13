from pathlib import Path
import shutil
from clipcompiler.media import command,probe
root=Path('verification'); root.mkdir(exist_ok=True)
source=root/'five-clips'; source.mkdir(exist_ok=True)
for name in ['Put it Down.mp4','gahgah.mp4','JFK.mp4']:
    shutil.copy2(Path(r'C:\Users\<user>\Videos')/name,source/name)
command(['ffmpeg','-v','error','-y','-i',source/'JFK.mp4','-ss','3','-t','8','-c:v','libx264','-c:a','aac',source/'JFK-offset.mp4'])
command(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=15:duration=6','-an','-c:v','libx264',source/'silent.mp4'])
for p in source.glob('*.mp4'):
    print(p.name,probe(p)['duration'])
