from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import websockets
from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

load_dotenv()
SCRIPT_LOCATION: Path = Path(__file__).parent
DATABASE_CONNECTION = sqlite3.connect(
    SCRIPT_LOCATION / "viewing_bookings.db", check_same_thread=False
)

booking_modal_json = (SCRIPT_LOCATION / "modals" / "booking_modal.json").read_text()

unbooking_modal_json = (SCRIPT_LOCATION / "modals" / "unbooking_modal.json").read_text()
UNBOOKING_MODAL = json.loads(unbooking_modal_json)

extending_modal_json = (SCRIPT_LOCATION / "modals" / "extending_modal.json").read_text()
EXTENDING_MODAL = json.loads(extending_modal_json)

no_bookings_modal_json = (
    SCRIPT_LOCATION / "modals" / "no_bookings_modal.json"
).read_text()

bookings_list_modal = (
    SCRIPT_LOCATION / "modals" / "bookings_list_modal.json"
).read_text()
BOOKINGS_MODAL = json.loads(bookings_list_modal)

app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"))
WEBSOCKET_PORT = os.getenv("WEBSOCKET_PORT")


class OverlappingBookingError(Exception):
    """Custom exception for when validation fails."""


@app.command("/viewing")
async def viewing_command(ack, command, client, context) -> None:
    """This function runs when a slack user uses the viewing command.

    Args:
        ack: Slack acknowledge
        command: Slack command
        client: Slack client
    """
    await ack()
    user_input = command["text"]

    if user_input.startswith("bookings"):
        await send_bookings_list_interface(client, command)

    elif user_input.startswith("book"):
        await client.views_open(
            trigger_id=command["trigger_id"], view=booking_modal_json
        )

    elif user_input.startswith("unbook"):
        await send_unbook_interface(client, command, context["user_id"])

    elif user_input.startswith("extend"):
        await send_extend_interface(client, command, context["user_id"])


@app.view("viewing_booking")
async def process_booking(ack, body, client, view) -> None:
    """This function processes a user booking from the booking modal.

    Args:
        ack: Slack acknowledge
        body: Slack body
        client: Slack client
        view: Slack view
    """
    booking_information = await sanitize_booking_input(
        view["state"]["values"], body["user"]["id"]
    )

    try:
        await add_booking(booking_information)
    except OverlappingBookingError as error_message:
        errors = {"booking_datetime": str(error_message)}
        await ack(response_action="errors", errors=errors)
        return

    await ack()

    human_readable_time = await get_readable_time_from_unix_time(
        booking_information["start_time"]
    )
    await client.chat_postMessage(
        channel=body["user"]["id"],
        text=f"Successfully booked viewing on {human_readable_time}.",
    )


@app.view("viewing_unbooking")
async def process_unbooking(ack, body, client, view) -> None:
    """This function processes a user unbooking from the unbooking modal.

    Args:
        ack: Slack acknowledge
        body: Slack body
        client: Slack client
        view: Slack view
    """
    unbooking_id = int(
        view["state"]["values"]["unbooking_select"]["unbooking_action"][
            "selected_option"
        ]["value"]
    )

    DATABASE_CONNECTION.execute("DELETE FROM bookings WHERE id = ?", (unbooking_id,))

    DATABASE_CONNECTION.commit()

    await ack()

    await client.chat_postMessage(
        channel=body["user"]["id"],
        text="Successfully removed booking.",
    )


@app.view("viewing_extending")
async def process_extending(ack, body, client, view) -> None:
    """This function processes a user extend from the extending modal.

    Args:
        ack: Slack acknowledge
        body: Slack body
        client: Slack client
        view: Slack view
    """
    extending_id = int(
        view["state"]["values"]["extending_select"]["extending_action"][
            "selected_option"
        ]["value"]
    )
    extension_in_seconds = (
        int(
            view["state"]["values"]["extending_duration"]["extending_duration"][
                "selected_option"
            ]["value"]
        )
        * 60
    )

    cursor = DATABASE_CONNECTION.cursor()
    cursor.execute("SELECT * FROM bookings WHERE id = ?", (extending_id,))
    booking = cursor.fetchone()

    if booking is None:
        await ack(
            response_action="errors", errors={"extending_select": "Booking not found."}
        )
        return

    new_end_time = booking[2] + extension_in_seconds
    cursor.execute(
        """
        SELECT * FROM bookings
        WHERE id != ? AND NOT (start_time >= ? OR end_time <= ?)
        """,
        (extending_id, new_end_time, booking[1]),
    )
    overlapping_booking = cursor.fetchone()

    if overlapping_booking:
        await ack(
            response_action="errors",
            errors={
                "extending_select": "Cannot extend booking as it overlaps with another booking."
            },
        )
        return

    cursor.execute(
        "UPDATE bookings SET end_time = ? WHERE id = ?", (new_end_time, extending_id)
    )
    DATABASE_CONNECTION.commit()

    await ack()

    await client.chat_postMessage(
        channel=body["user"]["id"],
        text="Successfully extended booking.",
    )


@app.view("no_bookings")
async def skip_no_booking(ack):
    """Runs when a user clicks okay in the 'no booking' modal.
    Thus, we can skip it here.

    Args:
        ack: Slack acknowledge
    """
    await ack()


@app.view("bookings_list")
async def skip_bookings_list(ack):
    """Runs when a user clicks okay in the 'bookings list' modal.
    Thus, we can skip it here.

    Args:
        ack: Slack acknowledge
    """
    await ack()


async def send_unbook_interface(client, command, user_id: str) -> None:
    """Sends the unbooking modal with correct booking information to the user.

    Args:
        client: Slack client
        command: Slack command
        user_id: ID of the user
    """
    user_bookings = await get_all_future_user_bookings(user_id)

    if user_bookings:
        input_options = await bookings_to_slack_options(user_bookings)

        new_unbooking_modal = UNBOOKING_MODAL
        new_unbooking_modal["blocks"][0]["element"]["options"] = input_options

        await client.views_open(
            trigger_id=command["trigger_id"], view=new_unbooking_modal
        )
        return

    await client.views_open(
        trigger_id=command["trigger_id"], view=no_bookings_modal_json
    )


async def send_extend_interface(client, command, user_id: str) -> None:
    """Sends the extending modal with correct booking information to the user.

    Args:
        client: Slack client
        command: Slack command
        user_id: ID of the user
    """
    user_bookings = await get_all_future_user_bookings(user_id)

    if user_bookings:
        input_options = await bookings_to_slack_options(user_bookings)

        new_extending_modal = EXTENDING_MODAL
        new_extending_modal["blocks"][0]["element"]["options"] = input_options

        await client.views_open(
            trigger_id=command["trigger_id"], view=new_extending_modal
        )
        return

    await client.views_open(
        trigger_id=command["trigger_id"], view=no_bookings_modal_json
    )


async def send_bookings_list_interface(client, command) -> None:
    """Sends a list of bookings items for the next week to the client.

    Args:
        client: Slack client
        command: Slack command
    """
    coming_week_bookings = await get_coming_week_bookings()
    blocks = await bookings_to_slack_list(coming_week_bookings)

    new_bookings_list_modal = BOOKINGS_MODAL
    new_bookings_list_modal["blocks"] = blocks

    await client.views_open(
        trigger_id=command["trigger_id"], view=new_bookings_list_modal
    )


async def sanitize_booking_input(
    view_state_values: dict, user_id: str
) -> dict[int, int, int, str, str]:
    """Parses the view state values and returns a nicer dictionary.

    Args:
        view_state_values: State values from view

    Returns:
        booking_information: Dict of relevant booking information
    """
    booking_information = {}
    booking_information["start_time"] = view_state_values["booking_datetime"][
        "booking_datetime"
    ]["selected_date_time"]

    booking_information["duration"] = int(
        view_state_values["booking_duration"]["booking_duration"]["selected_option"][
            "value"
        ]
    )

    booking_information["end_time"] = (
        booking_information["start_time"] + booking_information["duration"] * 60
    ) - 1

    booking_information["description"] = view_state_values["booking_description"][
        "booking_description"
    ]["value"]

    booking_information["user_id"] = user_id

    return booking_information


async def add_booking(booking_information: dict) -> None:
    """Adds the reservation to the booking database.

    Args:
        booking_information (dict): _description_

    Raises:
        OverlappingBookingError: Error when booking overlaps another one.
    """
    cursor = DATABASE_CONNECTION.cursor()
    cursor.execute(
        """
        SELECT * FROM bookings
        WHERE NOT (start_time >= ? OR end_time <= ?)
        """,
        (booking_information["end_time"], booking_information["start_time"]),
    )
    existing_booking = cursor.fetchone()

    if existing_booking:
        error_message = f"A booking called '{existing_booking[3]}' already exists during that time slot!"
        raise OverlappingBookingError(error_message)

    cursor.execute(
        """
            INSERT INTO bookings (start_time, end_time, description, user_id)
            VALUES (?, ?, ?, ?)
        """,
        (
            booking_information["start_time"],
            booking_information["end_time"],
            booking_information["description"],
            booking_information["user_id"],
        ),
    )

    DATABASE_CONNECTION.commit()


async def get_all_future_user_bookings(user_id: str) -> dict:
    """Returns all future (or ending in the future) bookings from user in the database.

    Args:
        user_id: Slack id of user

    Returns
        All future bookings from user
    """
    current_time = int(time.time())
    cursor = DATABASE_CONNECTION.cursor()
    cursor.execute(
        """
        SELECT * FROM bookings WHERE user_id = ? AND end_time > ? ORDER BY start_time ASC
    """,
        (user_id, current_time),
    )
    return cursor.fetchall()


async def get_coming_week_bookings() -> dict:
    """Returns all bookings from the start of the current day to one week after the start of today.

    Returns:
        All future bookings for the user within the specified time frame.
    """
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    one_week_later = today_start + timedelta(days=7)

    today_start_timestamp = int(today_start.timestamp())
    one_week_later_timestamp = int(one_week_later.timestamp())

    cursor = DATABASE_CONNECTION.cursor()
    cursor.execute(
        """
        SELECT * FROM bookings WHERE start_time >= ? AND start_time <= ? ORDER BY start_time ASC
        """,
        (today_start_timestamp, one_week_later_timestamp),
    )
    return cursor.fetchall()


async def get_current_three_bookings() -> dict:
    """Gets today's next three bookings, including a booking that is currently going on.

    Returns:
        Current coming three bookings
    """
    current_timestamp = int(datetime.now().timestamp())
    today_end_timestamp = int(
        datetime.now().replace(hour=23, minute=59, second=59, microsecond=0).timestamp()
    )

    cursor = DATABASE_CONNECTION.cursor()
    cursor.execute(
        """
        SELECT * FROM bookings WHERE end_time >= ? AND start_time <= ? ORDER BY start_time ASC LIMIT 3
        """,
        (current_timestamp, today_end_timestamp),
    )
    return cursor.fetchall()


async def bookings_to_slack_options(bookings: list) -> list:
    """Transforms a list of booking tuples into Slack Block Kit options format.

    Args:
        bookings: A list of tuples with booking information.

    Returns:
        A list of dictionaries formatted for Slack Block Kit's options field.
    """
    options = []
    for booking in bookings:
        booking_id, start_time, _, description, _ = booking
        readable_start_time = await get_readable_time_from_unix_time(start_time)

        option = {
            "text": {
                "type": "plain_text",
                "text": f"{description} - {readable_start_time}",
                "emoji": True,
            },
            "value": str(booking_id),
        }
        options.append(option)

    return options


async def bookings_to_slack_list(bookings: list) -> list:
    """Transforms a list of booking tuples into Slack Block Kit blocks for display.

    Args:
        bookings: A list of tuples with booking information.

    Returns:
        A list of dictionaries formatted for Slack Block Kit's blocks field.
    """
    blocks = []
    amsterdam = pytz.timezone("Europe/Amsterdam")
    last_date = None

    for booking in bookings:
        _, start_time, end_time, description, _ = booking

        start_time_datetime = (
            datetime.utcfromtimestamp(start_time)
            .replace(tzinfo=pytz.utc)
            .astimezone(amsterdam)
        )
        date_string = start_time_datetime.strftime("%B %d")

        start_time_string, end_time_string = await get_readable_start_end_time(
            start_time, end_time
        )

        if date_string != last_date:
            if last_date is not None:
                blocks.append({"type": "divider"})

            blocks.append(
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": date_string,
                        "emoji": True,
                    },
                }
            )

            last_date = date_string

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": f"{start_time_string}-{end_time_string}: {description}",
                    "emoji": True,
                },
            }
        )

    return blocks


async def bookings_to_view_json(current_three_bookings: list) -> str:
    """Turns three bookings into a nice JSON for use in our viewer.

    Args:
        current_three_bookings: List of current three bookings

    Returns:
        JSON of booking information
    """
    booking_information = {
        "current_booking": None,
        "first_coming_booking": None,
        "second_coming_booking": None,
    }

    if not current_three_bookings:
        return json.dumps(booking_information)

    for booking in current_three_bookings:
        _, start_time, end_time, description, _ = booking
        current_timestamp = int(datetime.now().timestamp())
        start_time_string, end_time_string = await get_readable_start_end_time(
            start_time, end_time
        )

        if not booking_information["current_booking"] and current_timestamp in range(
            start_time, end_time
        ):
            booking_information["current_booking"] = {
                "time": f"{start_time_string} - {end_time_string}",
                "description": description,
            }
            continue

        if not booking_information["first_coming_booking"]:
            booking_information["first_coming_booking"] = {
                "time": f"{start_time_string} - {end_time_string}",
                "description": description,
            }
            continue

        if not booking_information["second_coming_booking"]:
            booking_information["second_coming_booking"] = {
                "time": f"{start_time_string} - {end_time_string}",
                "description": description,
            }
            continue

    return json.dumps(booking_information)


async def get_readable_start_end_time(
    start_time: int, end_time: int
) -> tuple[str, str]:
    """Turns the start and end time integers into readable strings.

    Args:
        start_time: Start time
        end_time: End time

    Returns:
        start_time_string: H:M notation of time
        end_time_string: H:M notation of time
    """
    amsterdam = pytz.timezone("Europe/Amsterdam")

    start_time_datetime = (
        datetime.utcfromtimestamp(start_time)
        .replace(tzinfo=pytz.utc)
        .astimezone(amsterdam)
    )
    end_time_datetime = (
        datetime.utcfromtimestamp(end_time + 1)
        .replace(tzinfo=pytz.utc)
        .astimezone(amsterdam)
    )
    start_time_string = start_time_datetime.strftime("%H:%M")
    end_time_string = end_time_datetime.strftime("%H:%M")
    return start_time_string, end_time_string


async def get_readable_time_from_unix_time(unix_time: int) -> str:
    """Creates a readable string from unix time.

    Args:
        unix_time: Time in unix

    Returns:
        Human readable time string
    """
    amsterdam = pytz.timezone("Europe/Amsterdam")
    start_time_datetime = (
        datetime.utcfromtimestamp(unix_time)
        .replace(tzinfo=pytz.utc)
        .astimezone(amsterdam)
    )
    return start_time_datetime.strftime("%B %d: %H:%M")


async def create_bookings_table() -> None:
    """Creates a bookings table in the database if it does not already exist."""
    DATABASE_CONNECTION.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            description TEXT NOT NULL,
            user_id TEXT NOT NULL
        )
    """
    )
    DATABASE_CONNECTION.commit()


async def websocket_connection_handler(websocket) -> None:
    """This function handles the WebSocket connection.
    It sends an update to the connected screen every 10 seconds.

    Args:
        websocket: Websocket client instance
    """
    print("Screen has connected :)")
    while True:
        try:
            current_three_bookings = await get_current_three_bookings()
            json_to_send = await bookings_to_view_json(current_three_bookings)

            await websocket.send(json_to_send)

        except websockets.exceptions.ConnectionClosed:
            print("Screen has disconnected :(")
            return

        await asyncio.sleep(10)


async def start_websocket_server() -> None:
    """Starts the WebSocket server asynchronously,"""
    async with websockets.serve(websocket_connection_handler, "", WEBSOCKET_PORT):
        print("WebSocket server is running!")
        await asyncio.Future()


async def start_bolt_app() -> None:
    """Starts the Slack Bolt app asynchronously."""
    handler = AsyncSocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    await handler.start_async()


async def start_program() -> None:
    """Asynchronously starts the websocket server and bolt app."""
    await create_bookings_table()
    await asyncio.gather(
        start_websocket_server(),
        start_bolt_app(),
    )


if __name__ == "__main__":
    asyncio.run(start_program())
