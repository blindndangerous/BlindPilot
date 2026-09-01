# BlindPilot 0.7.1

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## A FreeBuff that never starts now says so

FreeBuff 0.0.163 can start, connect, and then paint nothing at all. It is still running
and still holding its connections, so nothing ever ends and nothing is ever drawn to type
a message into. This happens on a bare terminal exactly as readily as it does inside
BlindPilot, so it is FreeBuff's own fault rather than BlindPilot's — but BlindPilot
handled it badly.

A turn is given an hour to finish, and a terminal that neither dies nor reaches a prompt
used all of it. From the outside that was a message sent into complete silence, with the
failure finally spoken an hour later.

FreeBuff's start-up is now bounded by how long it goes without painting anything, rather
than by the clock. Any repaint counts as progress and starts the wait over, so a first
launch that is downloading and unpacking FreeBuff, showing a splash, or offering the model
picker is never cut off no matter how long it takes. A FreeBuff that has simply stopped is
reported after two minutes, quoting the last thing it managed to show, or saying plainly
that it showed nothing at all and suggesting FreeBuff be run in a terminal to confirm
where the fault lies.

## GLM 5.3 is the preferred FreeBuff model

FreeBuff has removed `deepseek/deepseek-v4-pro` from its catalogue, which is the model
BlindPilot preferred by default. GLM 5.3 (`z-ai/glm-5.3-flash`) takes its place.

This remains a preference rather than a requirement. FreeBuff drops and renames models
between releases, so BlindPilot still reads the catalogue out of the installed release at
run time, and a release that no longer offers GLM 5.3 falls back to a model that release
does offer instead of driving the picker toward a row that will never appear. A model
chosen explicitly in BlindPilot still wins over both.
