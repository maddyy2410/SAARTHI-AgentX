// =====================================================================
// api.js : the ONLY file that knows the backend's address.
// Every page talks to the backend through these functions, so if the
// backend moves, you change one line here and nothing else.
// =====================================================================
import { useEffect, useState } from "react"; // React "hooks": useState remembers values, useEffect runs code after drawing

export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000"; // backend address (from .env, or the default)
export const WS_URL = API_URL.replace(/^http/, "ws") + "/ws"; // live-update address: http -> ws, https -> wss

export async function getJSON(path) { // ask the backend for data (HTTP GET)
  const res = await fetch(API_URL + path); // send the request and wait for the reply
  if (!res.ok) throw new Error(`GET ${path} failed (${res.status})`); // anything other than success becomes an error
  return res.json(); // turn the reply text into JavaScript objects
}

export async function send(method, path, body) { // send a change (POST or PATCH) to the backend
  const res = await fetch(API_URL + path, { // send the request...
    method, // ...with this method ("POST" or "PATCH")
    headers: { "Content-Type": "application/json" }, // tell the backend we are sending JSON
    body: body ? JSON.stringify(body) : undefined, // the data, turned into JSON text (if there is any)
  });
  const data = await res.json().catch(() => ({})); // read the reply (empty object if it has no body)
  if (!res.ok) { // the backend refused (bad data, not found, conflict...)
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail); // readable reason
    throw new Error(detail || `${method} ${path} failed`); // pass the reason on so the page can show it
  }
  return data; // success: give back whatever the backend replied
}

// useApi: a custom hook. A page writes  const { data } = useApi("/api/alerts", version)
// and gets the data, automatically re-fetched every time "version" changes.
export function useApi(path, version) {
  const [data, setData] = useState(null); // the latest data (null until the first reply)
  const [error, setError] = useState(null); // an error message, if the request failed
  useEffect(() => { // runs after the page draws, and again whenever path or version changes
    let cancelled = false; // protects against a late reply arriving after the page has changed
    getJSON(path) // ask the backend
      .then((d) => { if (!cancelled) { setData(d); setError(null); } }) // store data, clear any old error
      .catch((e) => { if (!cancelled) setError(e.message); }); // store the error message
    return () => { cancelled = true; }; // "clean-up": ignore this request if a newer one starts
  }, [path, version]); // the list of values that trigger a re-fetch
  return { data, error }; // give both to the page
}
