"""Streamlit entry point. Run with: streamlit run app.py"""
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

from downloader import BASE, DownloadJob, Options, PRIVATE_MESSAGE, friendly, profile_info, username_value

st.set_page_config(page_title='Instagram Public Media Downloader', page_icon='📸', layout='wide')
st.markdown('''<style>
.block-container {max-width:1250px;padding-top:2.5rem}
[data-testid="stMetric"] {border:1px solid #dce3ec;border-radius:12px;padding:16px}
h1 {letter-spacing:-.035em}
</style>''', unsafe_allow_html=True)

for key, value in {'username':'', 'selected_post_count':100, 'download_status':'Idle',
                   'progress':0.0, 'statistics':{}, 'stop_state':False, 'errors':[],
                   'job':None, 'profile':None, 'profile_error':None}.items():
    if key not in st.session_state:
        st.session_state[key] = value

st.title('Instagram Public Media Downloader')
st.markdown('Bulk archive publicly accessible Instagram posts')
st.caption('Only download content you are authorized to archive or use.')

job = st.session_state.job
active = job is not None and not job.snapshot()['done']
with st.sidebar:
    st.header('Archive settings')
    username = st.text_input('Instagram Username', placeholder='example_account', key='username', disabled=active)
    check = st.button('Check Profile', disabled=active or not username.strip(), use_container_width=True)
    preset = st.selectbox('Number of posts', [100, 250, 500, 1000, 'Custom'], disabled=active)
    count = st.number_input('Custom number of posts', min_value=1, max_value=1000, value=100, step=1, disabled=active) if preset == 'Custom' else preset
    st.session_state.selected_post_count = int(count)
    media = st.selectbox('Media type', ['Images only', 'Images + Videos'], disabled=active)
    metadata = st.checkbox('Download metadata', value=True, disabled=active)
    folder = st.text_input('Output folder', value='Instagram_Downloads', disabled=active,
                           help='A folder name beneath the server downloads directory. Absolute paths and traversal are rejected.')
    start = st.button('Start Download', type='primary', disabled=active or not username.strip(), use_container_width=True)
    st.caption('Anonymous public access only. No passwords or Instagram login.')

if check:
    try:
        with st.spinner('Checking profile…'):
            st.session_state.profile = profile_info(username)
        st.session_state.profile_error = None
    except Exception as exc:
        st.session_state.profile = None
        st.session_state.profile_error = friendly(exc)
        st.session_state.errors = [f'Profile lookup: {type(exc).__name__} — {friendly(exc)}']

if st.session_state.profile_error:
    st.warning(st.session_state.profile_error)
profile = st.session_state.profile
if profile and profile['username'].lower() == username.strip().lstrip('@').lower():
    with st.container(border=True):
        photo, details = st.columns([1, 5])
        with photo:
            st.image(profile['profile_pic_url'], width=110)
        with details:
            st.subheader(profile['full_name'] or profile['username'])
            st.text('@' + profile['username'])
            st.text(profile['biography'] or 'No public biography available.')
            columns = st.columns(4)
            for col, label, value in zip(columns, ['Followers', 'Following', 'Posts', 'Visibility'],
                                          [profile['followers'], profile['followees'], profile['mediacount'],
                                           'Private' if profile['is_private'] else 'Public']):
                col.metric(label, value)
            if profile['is_private']:
                st.warning(PRIVATE_MESSAGE)

if start:
    try:
        options = Options(username=username_value(username), count=int(count), videos=media == 'Images + Videos',
                          metadata=metadata, folder=folder)
        new_job = DownloadJob(options)
        st.session_state.job = new_job
        st.session_state.stop_state = False
        st.session_state.errors = []
        new_job.start()
        st.rerun()
    except (ValueError, OSError) as exc:
        st.error(friendly(exc))


def open_folder(path):
    # Opt-in local desktop action. Never run shell text from user input.
    if os.environ.get('INSTAGRAM_ENABLE_OPEN_FOLDER') != '1':
        return
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


@st.fragment(run_every=1 if active else None)
def dashboard():
    current = st.session_state.job
    if not current:
        with st.container(border=True):
            st.subheader('Your archive, organized')
            st.write('Enter a public username, check the profile, and choose how many posts to archive.')
            st.write('Carousels count as one post. Completed files are reused when you request a larger archive.')
        if st.session_state.errors:
            with st.expander('Technical Details'):
                st.code('\n'.join(st.session_state.errors), language=None)
        return
    data = current.snapshot()
    st.session_state.download_status = data['status']
    st.session_state.progress = data['progress']
    st.session_state.statistics = {k: data[k] for k in ('processed', 'successful', 'failed', 'images', 'videos', 'skipped', 'reused')}
    st.session_state.errors = data['errors']
    with st.container(border=True):
        st.subheader('DOWNLOAD COMPLETE' if data['done'] and data['status'] == 'Complete' else data['status'].upper())
        st.write(data['message'])
        st.progress(min(1.0, max(0.0, data['progress'])), text=f"{data['processed']} of {current.options.count} posts processed · {data['progress']:.0%}")
        for row in [('processed', 'successful', 'failed'), ('images', 'videos', 'reused')]:
            cols = st.columns(3)
            names = {'processed':'Posts processed', 'successful':'Successful', 'failed':'Failed',
                     'images':'Images downloaded', 'videos':'Videos downloaded', 'reused':'Posts already on disk'}
            for col, key in zip(cols, row):
                col.metric(names[key], data[key])
        st.caption(f"Current shortcode: {data['current'] or '—'} · Status: {data['status']} · No matching media: {data['skipped']}")
        if not data['done']:
            if st.button('Stop Download', disabled=current.stop_event.is_set(), use_container_width=True):
                current.stop()
                st.session_state.stop_state = True
                st.rerun()
        else:
            if data['status'] == 'Complete' and not data['failed']:
                st.success(data['message'])
            elif data['status'] == 'Error':
                st.error(data['message'])
            else:
                st.warning(data['message'])
            st.caption(f'Output folder on the server: {current.directory}')
            archive = data['zip_path']
            if archive and Path(archive).is_file():
                size = Path(archive).stat().st_size
                st.caption(f'ZIP size: {size / (1024 * 1024):,.1f} MB. Contains this account’s complete saved folder, including earlier runs.')
                # Avoid eagerly loading multi-GB ZIPs on every rerun. Streamlit still holds
                # prepared downloads in memory, so impose an operator-configurable cap.
                cap = int(os.environ.get('INSTAGRAM_MAX_ZIP_MB', '512')) * 1024 * 1024
                if size <= cap:
                    with open(archive, 'rb') as stream:
                        st.download_button('Download ZIP', stream, file_name=f'Instagram_Download_{current.options.username}.zip',
                                           mime='application/zip', on_click='ignore', use_container_width=True)
                else:
                    st.warning('This ZIP exceeds the configured browser-download memory limit. Retrieve it from the server, or raise INSTAGRAM_MAX_ZIP_MB if sufficient RAM is available.')
                    st.code(archive, language=None)
            supported = os.environ.get('INSTAGRAM_ENABLE_OPEN_FOLDER') == '1'
            if st.button('Open Download Folder', disabled=not supported,
                         help='Opens a folder on the server desktop. Enable only for local desktop use.'):
                try:
                    open_folder(current.directory)
                except OSError:
                    st.warning('The operating system could not open the folder. Use the displayed path.')
        if data['errors']:
            with st.expander('Technical Details'):
                st.code('\n'.join(data['errors']), language=None)
    if data['done'] and active:
        st.rerun()


dashboard()
st.caption('Instagram may require login or limit anonymous requests, even for public profiles. The app stops safely when access is restricted.')
