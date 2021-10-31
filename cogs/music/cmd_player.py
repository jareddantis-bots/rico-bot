from collections import deque
from math import ceil, floor
from nextcord import Color, Embed
from nextcord.ext.commands import command, Context
from typing import Dict
from util import check_url, check_spotify_url, check_twitch_url, get_var, parse_spotify_url
from util import SpotifyInvalidURLError
from .queue_helpers import QueueItem, dequeue_db, enqueue, enqueue_db, get_queue_size, get_queue_index, set_queue_index, set_queue_db


@command()
async def loop(self, ctx: Context):
    # Get the player for this guild from cache.
    player = self.bot.lavalink.player_manager.get(ctx.guild.id)

    # Loop the current track.
    if player and (player.is_playing or player.paused):
        if not player.repeat:
            player.set_repeat(repeat=True)
            return await ctx.reply(':white_check_mark: **Now looping the current track**')
        player.set_repeat(repeat=False)
        return await ctx.reply(':white_check_mark: **No longer looping the current track**')
    return await ctx.reply('Not currently playing.')


@command(name='nowplaying', aliases=['np'])
async def now_playing(self, ctx: Context, track_info: Dict = None):
    # Delete the previous now playing message
    try:
        old_message_id = self.db.child('player').child(str(ctx.guild.id)).child('npmessage').get().val()
        if old_message_id:
            old_message = await ctx.fetch_message(int(old_message_id))
            await old_message.delete()
    except Exception as e:
        print(f'Error while trying to delete old npmsg: {e}')

    # Get the player for this guild from cache
    player = self.bot.lavalink.player_manager.get(ctx.guild.id)

    if player.is_playing or player.paused:
        embed = Embed(color=Color.teal())

        # Get requester info
        requester = await self.bot.fetch_user(player.current.requester)
        embed.set_footer(text=f'Requested by {requester.name}#{requester.discriminator}')

        # Try to recover track info
        progress = None
        if track_info is None:
            # Invoked by command
            current_id = player.current.identifier
            stored_info = player.fetch(current_id)
            if stored_info and 'title' in stored_info:
                track_info = stored_info

                # Don't create progress info for streams
                if not check_twitch_url(track_info['uri']) and not track_info['isStream']:
                    # Create progress text
                    total_ms = track_info['length']
                    total_m, total_s = divmod(floor(total_ms / 1000), 60)
                    total_text = f'{total_m:02d}:{total_s:02d}'
                    elapsed_ms = player.position
                    elapsed_m, elapsed_s = divmod(floor(elapsed_ms / 1000), 60)
                    elapsed_text = f'{elapsed_m:02d}:{elapsed_s:02d}'

                    # Create progress bar
                    total = 20
                    elapsed_perc = elapsed_ms / total_ms
                    elapsed = '-' * (ceil(elapsed_perc * total) - 1)
                    remain = ' ' * floor((1 - elapsed_perc) * total)
                    progress_bar = f'`[{elapsed}O{remain}]`'

                    # Build progress info
                    progress = f'\n**{elapsed_text} {progress_bar} {total_text}**'
        else:
            # Invoked by listener
            # Don't create progress info for streams
            if check_twitch_url(track_info['uri']) and not track_info['isStream']:
                m, s = divmod(floor(track_info['length'] / 1000), 60)
                progress = f'{m:02d} min, {s:02d} sec'
        
        if track_info is not None:
            # Show if track is a live stream
            current_action = 'streaming' if 'isStream' in track_info and track_info['isStream'] else 'playing'
            embed.title = 'Paused' if player.paused else f'Now {current_action}'

            # Show rich track info
            track_name = track_info['title']
            track_artist = track_info['author']
            track_uri = track_info['uri']
            if hasattr(track_info, 'spotify'):
                track_name = track_info['spotify']['name']
                track_artist = track_info['spotify']['artist']
                track_uri = f'https://open.spotify.com/track/{track_info["spotify"]["id"]}'
            embed.description = '\n'.join([
                f'**[{track_name}]({track_uri})**',
                f'by {track_artist}',
                progress if progress is not None else ''
            ])
        else:
            # Show basic track info
            embed.title = 'Paused' if player.paused else 'Now playing'
            embed.description = player.current.title
    else:
        embed = Embed(color=Color.yellow())
        embed.title = 'Not playing'
        embed.description = 'To play, use `{0}play <URL/search term>`. Try `{0}help` for more.'.format(get_var('BOT_PREFIX'))

    # Save this message
    if track_info is not None:
        message = await ctx.send(embed=embed)
    else:
        message = await ctx.reply(embed=embed)
    self.db.child('player').child(str(ctx.guild.id)).child('npmessage').set(str(message.id))


@command()
async def pause(self, ctx: Context):
    # Get the player for this guild from cache.
    player = self.bot.lavalink.player_manager.get(ctx.guild.id)

    # Pause the player.
    if not player.paused:
        await player.set_pause(pause=True)
        await ctx.reply('Paused the player.')
    else:
        await ctx.reply('Already paused.')


@command(aliases=['p'])
async def play(self, ctx: Context, *, query: str = None):
    """ Searches and plays a song from a given query. """
    async with ctx.typing():
        if not query:
            # Pick up where we left off
            old_np = get_queue_index(self.db, str(ctx.guild.id))
            if isinstance(old_np, int):
                # Send resuming queue embed
                embed = Embed(color=Color.purple(), title='Resuming interrupted queue')
                await ctx.reply(embed=embed)

                # Play at index
                track = dequeue_db(self.db, str(ctx.guild.id), old_np)
                return await enqueue(self.bot, track, ctx=ctx, quiet=True)
            return await ctx.reply('Please specify a URL or a search term to play.')
        else:
            # Clear previous queue if not currently playing
            player = self.bot.lavalink.player_manager.get(ctx.guild.id)
            if player is not None and not (player.is_playing or player.paused):
                set_queue_db(self.db, str(ctx.guild.id), [])

        # Remove leading and trailing <>.
        # <> may be used to suppress embedding links in Discord.
        query = query.strip('<>')
        new_tracks = []
        if check_spotify_url(query):
            # Query is a Spotify URL.
            try:
                sp_type, sp_id = parse_spotify_url(query, valid_types=['track', 'album', 'playlist'])
            except SpotifyInvalidURLError:
                return await ctx.reply('Only Spotify track, album, and playlist URLs are supported.')

            if sp_type == 'track':
                # Get track details from Spotify
                track_name, track_artist, track_id = self.spotify.get_track(sp_id)

                # Add to database queue
                new_tracks.append(QueueItem(
                    requester=ctx.author.id,
                    title=track_name,
                    artist=track_artist,
                    spotify_id=track_id
                ))
            else:
                # Get playlist or album tracks from Spotify
                list_name, list_author, tracks = self.spotify.get_tracks(sp_type, sp_id)
                track_queue = deque(tracks)

                # Send enqueueing embed
                embed = Embed(color=Color.blurple())
                embed.title = f'Enqueueing Spotify {sp_type}'
                embed.description = f'[{list_name}]({query}) by {list_author} ({len(tracks)} tracks)'
                embed.set_footer(text='This might take a while, please wait.')
                await ctx.send(embed=embed)

                if len(tracks) < 1:
                    # No tracks
                    return await ctx.reply(f'Spotify {sp_type} is empty.')
                elif len(tracks) == 1:
                    # Single track
                    track_name, track_artist, track_id = tracks[0]
                    new_tracks.append(QueueItem(
                        requester=ctx.author.id,
                        title=track_name,
                        artist=track_artist,
                        spotify_id=track_id
                    ))
                else:
                    # Multiple tracks
                    for track in track_queue:
                        track_name, track_artist, track_id = track
                        new_tracks.append(QueueItem(
                            requester=ctx.author.id,
                            title=track_name,
                            artist=track_artist,
                            spotify_id=track_id
                        ))
        elif check_url(query):
            # Query is a non-Spotify URL.
            new_tracks.append(QueueItem(
                requester=ctx.author.id,
                url=query
            ))
        else:
            # Query is not a URL.
            if query.startswith('ytsearch:') or query.startswith('scsearch:'):
                # Query begins with the search modifiers 'ytsearch' or 'scsearch'
                new_tracks.append(QueueItem(
                    requester=ctx.author.id,
                    query=query
                ))
            else:
                # Have Lavalink do a YouTube search for the query
                new_tracks.append(QueueItem(
                    requester=ctx.author.id,
                    query=f'ytsearch:{query}'
                ))

        if len(new_tracks):
            # Add new tracks to queue
            enqueue_db(self.db, str(ctx.guild.id), new_tracks)

            # Send embed
            embed = Embed(color=Color.blurple())
            embed.title = f'Added to queue'
            embed.description = f'{len(new_tracks)} item(s)'
            await ctx.reply(embed=embed)

            # Play the first track
            set_queue_index(self.db, str(ctx.guild.id), 0)
            await enqueue(self.bot, new_tracks[0], ctx, False)


@command(aliases=['next'])
async def skip(self, ctx: Context, queue_end: bool = False):
    async with ctx.typing():
        # Get the player for this guild from cache.
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        # Queue up the next (valid) track from DB, if any
        next_i = get_queue_index(self.db, str(ctx.guild.id)) + 1
        queue_size = get_queue_size(self.db, str(ctx.guild.id))
        while next_i < queue_size:
            track = dequeue_db(self.db, str(ctx.guild.id), next_i)
            
            try:
                if await enqueue(self.bot, track, ctx=ctx, quiet=True):
                    if not queue_end:
                        await player.skip()
                    break
            except Exception as e:
                return await ctx.send(f'Unable to play {track}. Reason: {e}')
            finally:
                next_i += 1
        else:
            if not queue_end:
                # Remove player data from DB
                self.db.child('player').child(str(ctx.guild.id)).remove()
                return await self.disconnect(ctx, reason='Reached the end of the queue')

        # Save new queue index back to db
        set_queue_index(self.db, str(ctx.guild.id), next_i)


@command()
async def unpause(self, ctx: Context):
    # Get the player for this guild from cache.
    player = self.bot.lavalink.player_manager.get(ctx.guild.id)

    # Unpause the player.
    if player.paused:
        await player.set_pause(pause=False)
        await ctx.reply('Unpaused the player.')
    else:
        await ctx.reply('Already unpaused.')


@command(aliases=['v', 'vol'])
async def volume(self, ctx: Context, *, vol: str = None):
    if vol is None:
        # Get the player for this guild from cache.
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        if player is not None:
            # Return current player volume
            embed = Embed(color=Color.purple())
            embed.title = f':loud_sound: Volume is currently at {player.volume}'
            embed.description = f'To set, use `{get_var("BOT_PREFIX")}{ctx.invoked_with} <int>`.'
            return await ctx.reply(embed=embed)
        return await ctx.reply(f'No active players in {ctx.guild.name}')

    try:
        new_vol = int(vol)
        if new_vol < 0 or new_vol > 1000:
            raise ValueError
    except ValueError:
        return await ctx.reply('Please specify an integer between 0 and 1000, inclusive.')
    
    # Get the player for this guild from cache.
    player = self.bot.lavalink.player_manager.get(ctx.guild.id)
    if player is not None and player.is_playing and not player.paused:
        await player.set_volume(new_vol)
        return await ctx.reply(f':white_check_mark: Volume set to **{new_vol}**')
    return await ctx.reply('Player is not playing or is paused')
