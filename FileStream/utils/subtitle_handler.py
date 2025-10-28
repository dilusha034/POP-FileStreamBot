import asyncio
import json
import logging

async def get_subtitle_streams(file_path):
    command = [
        'ffprobe',
        '-v', 'error',
        '-print_format', 'json',
        '-show_streams',
        '-select_streams', 's',
        file_path
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if stderr:
            logging.error(f"FFprobe stderr: {stderr.decode()}")
        
        data = json.loads(stdout)
        subtitles = []
        for stream in data.get('streams', []):
            lang_code = stream.get('tags', {}).get('language', 'und')
            subtitles.append({
                'index': stream['index'],
                'language': lang_code,
                'label': lang_code.upper()
            })
        logging.info(f"Found subtitles for {file_path}: {subtitles}")
        return subtitles
    except Exception as e:
        logging.error(f"Error in get_subtitle_streams: {e}")
        return []
