# The slack booking bot for our viewing room
At the Netherlands Filmacademy we have this very nice viewing room where we can host our VFX review sessions. This is all very nice, however recently we've been running into some _scheduling conflicts_. You see, a lot of students want to use the space and we really didn't really communicate about when the space was in use. This was causing some minor conflicts, so I decided to come up with a solution. My first solution was simple, I wanted school to just buy Joan (a very nice room booking system). School deemed that a little too expensive though, which was a shame because many students wanted to use a system like it. That's why I decided to create a simple system inspired by Joan. Students can now book our viewing room through Slack, and reservations are displayed on a screen that is now mounted on the door. The Slack App was made with Slack Bolt for Python and the screen is just an old Android phone running a React Native app I wrote.

### Slack App UI
<p align="center">
  <img src="https://github.com/BreakTools/slack-booking-bot/assets/63094424/ccee3152-6bc1-41d9-bd8d-53dd413bd16e" />
</p>

### Screen above door
<p align="center">
  <img src="https://github.com/BreakTools/slack-booking-bot/assets/63094424/6e6b9c86-2c16-446d-a056-73cd432bda2a" />
</p>

# How it works
It's quite a simple system:
- The python script hosts the Slack App and a websocket server
- The screen connects to the websocket server
- Users book the room through Slack
- Reservations get stored in a database
- The screen updates according to the reservations

  
