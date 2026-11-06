// =====================================================================
// Chart.jsx : one wrapper around Plotly (the chart library in our notes)
// Every page draws charts through <Chart data={...} layout={...} />
// =====================================================================
import createPlotlyComponentModule from "react-plotly.js/factory"; // builds a React component from Plotly
import Plotly from "plotly.js-dist-min"; // the small ready-made Plotly bundle

const createPlotlyComponent = createPlotlyComponentModule.default || createPlotlyComponentModule; // works with every bundler setting
const Plot = createPlotlyComponent(Plotly); // the React <Plot> component

export default function Chart({ data, layout = {}, height = 300 }) { // data = Plotly traces, layout = extra settings
  return (
    <Plot
      data={data} // the lines or bars to draw
      layout={{ // shared look for every chart
        autosize: true, // fill the width of its box
        height, // fixed height in pixels
        margin: { l: 56, r: 16, t: 10, b: 44 }, // room for axis labels
        paper_bgcolor: "rgba(0,0,0,0)", // transparent: the card colour shows through
        plot_bgcolor: "rgba(0,0,0,0)", // same for the plotting area
        font: { family: "IBM Plex Sans, system-ui, sans-serif", size: 12, color: "#3f4450" }, // text style
        legend: { orientation: "h", y: -0.2 }, // legend below the chart
        ...layout, // page-specific settings win
      }}
      config={{ displayModeBar: false, responsive: true }} // hide the toolbar, resize with the window
      useResizeHandler // redraw when the window size changes
      style={{ width: "100%" }} // take the full card width
    />
  );
}
