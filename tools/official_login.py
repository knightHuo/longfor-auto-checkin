"""Capture a user-operated official SMS session directly into Windows Credential Manager."""
import ctypes
import json
import os
import re
import subprocess
from pathlib import Path

from panel_credentials import Credential, _api

TARGET = 'Codex/LongforAutoCheckin/MemberSession'
SESSION_SOURCE = 'official-sms-lzb'


def save_session(value):
    if not isinstance(value,dict) or value.get('source') != SESSION_SOURCE:
        raise ValueError('Expected an official member session')
    token=value.get('token')
    if not isinstance(token,str) or not re.fullmatch(r'[0-9a-fA-F]{32}',token):
        raise ValueError('No usable official member token')
    payload=json.dumps({'token':token,'user_agent':value.get('user_agent',''),
                        'source':SESSION_SOURCE,'channel':'C2','bu_code':'C20400'})
    blob=payload.encode('utf-16-le')
    if len(blob)>2560:
        raise ValueError('Session is too large for the credential store')
    buffer=(ctypes.c_ubyte*len(blob)).from_buffer_copy(blob)
    item=Credential(Type=1,TargetName=TARGET,UserName='member',Persist=2,
                    Comment='User-operated official Longfor session',
                    CredentialBlobSize=len(blob),CredentialBlob=buffer)
    if not _api().CredWriteW(ctypes.byref(item),0):
        raise OSError(ctypes.get_last_error(),'Could not store member session')


def main():
    if os.name!='nt':
        raise RuntimeError('Use this helper on the Windows workstation')
    process=subprocess.Popen(['node',str(Path(__file__).with_suffix('.cjs'))],
                             stdout=subprocess.PIPE,text=True,encoding='utf-8',
                             creationflags=subprocess.CREATE_NO_WINDOW)
    saved=False
    for line in process.stdout:
        try:
            value=json.loads(line)
            if isinstance(value,dict) and value.get('token'):
                save_session(value)
                saved=True
                print(json.dumps({'saved':True,'credential_target':TARGET}),flush=True)
        except (ValueError,TypeError):
            print('The returned session could not be stored; no credential was printed.',flush=True)
    code=process.wait()
    if code or not saved:
        raise SystemExit('Official login did not produce a stored member session')


if __name__=='__main__':
    main()
