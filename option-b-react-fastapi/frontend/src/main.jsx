// main.jsx : the starting point. It puts the <App /> component into index.html
import React from "react"; // the React library
import ReactDOM from "react-dom/client"; // the part of React that draws in the browser
import "leaflet/dist/leaflet.css"; // styles the map needs to display correctly
import "./styles.css"; // our own styles
import App from "./App.jsx"; // our main component

ReactDOM.createRoot(document.getElementById("root")).render( // find <div id="root"> and draw inside it...
  <React.StrictMode> {/* StrictMode shows extra warnings while developing */}
    <App /> {/* ...our whole application */}
  </React.StrictMode>
);
