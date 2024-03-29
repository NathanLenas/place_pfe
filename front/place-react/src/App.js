import axios from "axios";
import Canvas from './canvas';
import Connection from "./connection";
import { BrowserRouter as Router, Route, Routes } from 'react-router-dom';
import Register from "./register";
function App() {

  // fetchBoard();
  return (
    <Router>
      <Routes>
        <Route path="/" element={<Connection />} />
        <Route path="/canvas" element={<Canvas />} />
        <Route path="/register" element={<Register />} />
      </Routes>
    </Router>
  );
}

export default App;
