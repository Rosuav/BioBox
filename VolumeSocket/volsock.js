//In theory, this could use declarativeContent to look for a video tag
//How does that signal that a page no longer meets the criterion?
//NOTE: This broadly assumes only one important video object, which is
//always present. It might work with multiple but isn't guaranteed.

//Content scripts aren't allowed to see the actual tab ID, so hack in an unlikely-to-be-duplicated one
const tabid = Math.random() + "" + Math.random();
let retry_delay = 5000;
function connect()
{
	let socket = new WebSocket("ws://localhost:8888/ws");
	socket.onopen = () => {
		retry_delay = 0;
		console.log("VolSock connection established.");
		socket.send(JSON.stringify({cmd: "init", type: "volume", group: tabid}));
		document.querySelectorAll("video").forEach(vid =>
			(vid.onvolumechange = e => socket.send(JSON.stringify({cmd: "setvolume", volume: vid.volume})))()
		);
	};
	socket.onclose = () => {
		console.log("VolSock connection lost.");
		setTimeout(connect, retry_delay || 250);
		if (retry_delay < 30000) retry_delay += 5000;
	};
	socket.onmessage = (ev) => {
		let data = JSON.parse(ev.data);
		if (data.cmd === "setvolume") {
			document.querySelectorAll("video").forEach(vid => vid.volume = data.volume);
		}
		//TODO: Use the muted flag to control muting of the entire tab? Do I have permission to
		//do that? Might need a separate background script. Seems overkill, although it would
		//also mean we get real tab IDs.
	};
}
if (document.readyState !== "loading") connect();
else window.addEventListener("DOMContentLoaded", connect);
