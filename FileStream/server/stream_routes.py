import aiofiles
from FileStream.utils.subtitle_handler import get_subtitle_streams
import aiofiles.os as aio_os
import json
import time
import math
import logging
import mimetypes
import traceback
import asyncio
import subprocess
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream.config import Telegram
from FileStream.server.exceptions import FIleNotFound, InvalidHash
from FileStream import utils, StartTime, __version__
from FileStream.utils.render_template import render_page

routes = web.RouteTableDef()
class_cache = {}

# --- Status Route එක (වෙනසක් නැහැ) ---
@routes.get("/status", allow_head=True)
async def status_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "telegram_bot": "@" + FileStream.username,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

# --- Web Player Page එක (වෙනසක් නැහැ) ---
@routes.get("/watch/{path}", allow_head=True)
async def watch_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return web.Response(text=await render_page(path), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)

# --- START: මෙතනින් තමයි ලොකුම වෙනස පටන් ගන්නේ ---

# --- උපසිරැසි තොරතුරු ලබාදෙන, වේගය වැඩි කල Route එක ---
@routes.get("/subtitles/{db_id}")
async def subtitles_json_handler(request: web.Request):
    try:
        db_id = request.match_info['db_id']
        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]
        
        tg_connect = class_cache.get(faster_client)
        if not tg_connect:
            tg_connect = utils.ByteStreamer(faster_client)
            class_cache[faster_client] = tg_connect

        file_id = await tg_connect.get_file_properties(db_id, multi_clients)
        temp_file_path = f"/tmp/{db_id}"

        # --- මෙන්න අපේ වේගය වැඩි කිරීමේ මෙහෙයුම ---
        
        chunk_size = 1024 * 1024 
        
        # --- අපි වෙනස් කරන එකම එක පේළිය! ---
        # 5MB වෙනුවට දැන් අපි 2MB ක් විතරක් download කරනවා.
        total_bytes_to_download = 2 * 1024 * 1024 # 5MB සිට 2MB දක්වා අඩු කළා

        parts_to_fetch = math.ceil(total_bytes_to_download / chunk_size)

        streamer = tg_connect.yield_file(
            file_id,
            index,
            offset=0,
            first_part_cut=0,
            last_part_cut=chunk_size, 
            part_count=parts_to_fetch,
            chunk_size=chunk_size
        )
        
        async with aiofiles.open(temp_file_path, "wb") as f:
            async for chunk in streamer:
                await f.write(chunk)
        
        subtitles = await get_subtitle_streams(temp_file_path)
        
        await aio_os.remove(temp_file_path)
        
        return web.json_response(subtitles)
    except Exception as e:
        logging.error(f"FATAL ERROR in subtitles_json_handler: {e}", exc_info=True)
        return web.json_response([])

# --- User තෝරන උපසිරැසිය දෙන, දියුණු කල Subtitle Route එක ---
@routes.get("/subtitle/{db_id}")
async def subtitle_handler(request: web.Request):
    try:
        db_id = request.match_info['db_id']
        # JavaScript එකෙන් එවන index අංකය මෙතනින් අල්ලගන්නවා
        sub_index = request.query.get("index")

        if not sub_index:
            return web.Response(status=400, text="Subtitle index not provided")

        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]
        tg_connect = class_cache.get(faster_client, utils.ByteStreamer(faster_client))
        file_id = await tg_connect.get_file_properties(db_id, multi_clients)

        # User තෝරපු index එකට අදාළව ffmpeg command එක හදනවා
        map_param = f"0:{sub_index}"
        command = ['ffmpeg', '-i', 'pipe:0', '-map', map_param, '-f', 'webvtt', 'pipe:1']
        
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        response = web.StreamResponse(headers={'Content-Type': 'text/vtt'})
        await response.prepare(request)

        # --- පහළ තියෙන streaming logic එකේ වෙනසක් කරන්නේ නැහැ ---
        async def pipe_to_ffmpeg():
            try:
                async for chunk in tg_connect.yield_file(file_id, index, 0, 0, file_id.file_size, math.ceil(file_id.file_size / (1024*1024)), 1024*1024):
                    if process.stdin.is_closing(): break
                    process.stdin.write(chunk)
                    await process.stdin.drain()
            except (asyncio.CancelledError, ConnectionResetError):
                pass
            finally:
                if not process.stdin.is_closing(): process.stdin.close()

        async def pipe_to_client():
            try:
                while not process.stdout.at_eof():
                    chunk = await process.stdout.read(4096)
                    if not chunk: break
                    await response.write(chunk)
            except (asyncio.CancelledError, ConnectionResetError):
                pass
        
        async def log_stderr():
            while not process.stderr.at_eof():
                line = await process.stderr.readline()
                if line: logging.error(f"FFmpeg Subtitle stderr: {line.decode().strip()}")

        await asyncio.gather(pipe_to_ffmpeg(), pipe_to_client(), log_stderr())
        await response.write_eof()
        return response

    except Exception as e:
        logging.error(f"Subtitle generation failed: {e}")
        return web.Response(status=500, text="Failed to extract subtitle.")

# --- END: ලොකුම වෙනස මෙතනින් ඉවරයි ---


# --- Video/File Download Route එක (වෙනසක් නැහැ) ---
@routes.get("/dl/{path}", allow_head=True)
async def download_handler(request: web.Request):
    try:
        return await media_streamer(request, request.match_info["path"])
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

# --- Media Streamer (Core Logic) (වෙනසක් නැහැ) ---
async def media_streamer(request: web.Request, db_id: str):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if Telegram.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.headers.get('X-FORWARDED-FOR',request.remote)}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
    else:
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
        
    file_id = await tg_connect.get_file_properties(db_id, multi_clients)
    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(status=416, body="416: Range not satisfiable", headers={"Content-Range": f"bytes */{file_size}"})

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)
    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1
    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size)

    mime_type = file_id.mime_type
    file_name = utils.get_name(file_id)
    disposition = "inline"

    if not mime_type:
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
