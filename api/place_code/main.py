from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import PlainTextResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from redis import Redis
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi import WebSocket, WebSocketDisconnect
from cassandra.cluster import Cluster, Session
import time
import os
from datetime import datetime
from typing import Optional, Set
import logging
from fastapi_utils.timing import add_timing_middleware, record_timing


## Constants 
ENABLE_TIMING = True
DELAY = 2 # Delay in seconds for every pixel update

# Board
MAX_COLORS =  16
BOARD_SIZE =  100


# Setup logging 

# Create log directory if it does not exist
if not os.path.exists("/code/app/log"):
    os.makedirs("/code/app/log")

# Create log file if it does not exist
if not os.path.exists("/code/app/log/place.log"):
    with open("/code/app/log/place.log", "w") as f:
        f.write("")
        
# Token constants 
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is not set.")

ALGORITHM = os.getenv("ALGORITHM")
if not ALGORITHM:
    raise RuntimeError("ALGORITHM environment variable is not set.")

# Connection constants for Redis and Cassandra
redis_host = os.getenv("REDIS_HOST", "redis") # Get the REDIS_HOST environment variable coming from the docker-compose file
redis_port = int(os.getenv("REDIS_PORT", 6379))

cassandra_host = os.getenv("CASSANDRA_HOST", "cassandra")  # Get the CASSANDRA_HOST environment variable coming from the docker-compose file
cassandra_port = int(os.getenv("CASSANDRA_PORT", 9042))

# Define JWT settings
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

# Helper functions
def setup_middleware(app: FastAPI):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    if ENABLE_TIMING:
        add_timing_middleware(app, record=logger.info, prefix="app", exclude="untimed")


class TokenData(BaseModel):
    username: str = None


def connect_to_cassandra(retries:int=10) -> Optional[Session]:
    for _ in range(retries):
        try:
            cluster = Cluster([cassandra_host], port=cassandra_port)   
            session = cluster.connect()  
            return session
        except Exception as e:
            time.sleep(10)  # Wait for 10 seconds before retrying
    raise RuntimeError("Failed to connect to Cassandra after several attempts.")

def connect_to_redis(retries:int=10) -> Redis:
    for attempt in range(retries):
        try:
            return Redis(host=redis_host, port=redis_port, decode_responses=False)
        except Exception as e:
            time.sleep(10)  # Wait for 10 seconds before retrying
            print(e)
    raise RuntimeError("Failed to connect to Redis after several attempts.")

def init_cassandra_db(cassandra_session : Session):
    if cassandra_session:
        print("Creating tables")
        # Create keyspace and table
        cassandra_session.execute("CREATE KEYSPACE IF NOT EXISTS place WITH replication = {  'class' : 'SimpleStrategy',  'replication_factor' :  1};")
        cassandra_session.execute("CREATE TABLE IF NOT EXISTS place.tiles (x int, y int, color int, user text, timestamp timestamp, PRIMARY KEY ((x, y)));")
        cassandra_session.execute("CREATE TABLE IF NOT EXISTS place.last_tile_timestamp (user text PRIMARY KEY, timestamp timestamp);")
    else:
        print("Failed to connect to Cassandra. Exiting.")


class DrawCommand(BaseModel):
    x: int
    y: int
    color: int

    
def create_bitmap(redis_client : Redis, key : str, total_pixels:int=10000):
    for i in range(total_pixels):
        offset = i * 4
        bf = redis_client.bitfield(key)
        bf.set('u4', offset, 0)
        bf.execute()
        

def set_last_user_timestamp(cassandra_session: Session, user: str, timestamp: Optional[datetime] = None):
    if timestamp is None:
        timestamp = datetime.utcnow()  # Use current UTC time if none provided

    # Separate the update and insert operations
    cassandra_session.execute("UPDATE place.last_tile_timestamp SET timestamp = %s WHERE user = %s", (timestamp, user))
    cassandra_session.execute("INSERT INTO place.last_tile_timestamp (user, timestamp) VALUES (%s, %s) IF NOT EXISTS", (user, timestamp))


def get_last_user_timestamp(cassandra_session: Session, user: str):
    query = f"SELECT timestamp FROM place.last_tile_timestamp WHERE user = '{user}'"
    prepared_query = cassandra_session.prepare(query)
    result = cassandra_session.execute(prepared_query)
    row = result.one()
    return row.timestamp if row else None


def store_draw_info(cassandra_session: Session, x: int, y: int, color: int, user: str, timestamp: Optional[datetime] = None):
    # If timestamp is None, use the current timestamp
    if timestamp is None:
        timestamp = datetime.utcnow()

    cassandra_session.execute("INSERT INTO place.tiles (x, y, color, user, timestamp) VALUES (%s, %s, %s, %s, %s)", (x,y,color,user,timestamp))    


def get_bitfield_as_integers(redis_client : Redis, key : str, total_integers:int=BOARD_SIZE*BOARD_SIZE):
    bitstring = redis_client.get(key)
    if not bitstring:
        return []
    bitfield = []
    for byte in bitstring:
        upper_nibble = byte >> 4
        lower_nibble = byte & 0x0F
        bitfield.extend([upper_nibble, lower_nibble])
    return bitfield[:total_integers]


def get_pixel_color(redis_client:Redis, key:str, x:int, y:int):
    index = x + y * BOARD_SIZE
    byte_index = index // 2
    byte_data = redis_client.getrange(key, byte_index, byte_index)
    if not byte_data:
        return None
    is_upper_nibble = index % 2 == 0
    if is_upper_nibble:
        return (byte_data[0] >> 4) & 0x0F
    else:
        return byte_data[0] & 0x0F


def set_4bit_value(redis_client:Redis, key:str, index:int, value:int):
    byte_index = index // 2
    current_byte_data = redis_client.getrange(key, byte_index, byte_index)
    current_byte = int.from_bytes(current_byte_data, 'big') if current_byte_data else 0
    is_upper_nibble = index % 2 == 0
    if is_upper_nibble:
        new_byte = ((value & 0x0F) << 4) | (current_byte & 0x0F)
    else:
        new_byte = (current_byte & 0xF0) | (value & 0x0F)
    redis_client.setrange(key, byte_index, new_byte.to_bytes(1, byteorder='big'))

def decode_jwt(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            print("Username is None")
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        print("JWTError")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return token_data
    
# --------------------------------------------------------------------------------------------

        
logging.basicConfig(level=logging.INFO, filename="/code/app/log/place.log")
logger = logging.getLogger(__name__)

# App instance creation
app = FastAPI()

static_files_app = StaticFiles(directory=".")
app.mount(path="/static", app=static_files_app, name="static")

# Database connections
redis_session = connect_to_redis(10)
cassandra_session = connect_to_cassandra(10)

# Global variables
key = 'place_bitmap'

active_connections: Set[WebSocket] = set()

# Middleware setup
setup_middleware(app)

# Create bitmap on startup
create_bitmap(redis_session, key)

init_cassandra_db(cassandra_session)

# --------------------------------------------------------------------------------------------
# Models
class DrawCommand(BaseModel):
    x: int
    y: int
    color: int

    
# --------------------------------------------------------------------------------------------
# Routes

app = FastAPI()
if ENABLE_TIMING:
    add_timing_middleware(app, record=logger.info, prefix="app", exclude="untimed")

static_files_app = StaticFiles(directory=".")
app.mount(path="/static", app=static_files_app, name="static")

@app.middleware("http")
async def verify_token(request: Request, call_next):
    # Allow access to the documentation without a token
    path = request.url.path
    if path == "/redoc" or path == "/docs" or path == "/openapi.json" or path == "/" or path == "/auth/token" or path == "/api/place/board-bitmap/ws":
        response = await call_next(request)
        return response
    
    # Extract the token from the Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        print("No auth header")
        return PlainTextResponse("You are not authenticated. Please provide a token in the 'Authorization' header.", status_code=401)
    
    # Decode and verify the token
    try:
        token_data = decode_jwt(auth_header)
    except Exception as e:
        print("Token error : ", e)
        return PlainTextResponse("Your token is invalid. Please provide a valid token in the 'Authorization' header.", status_code=401)
    
    # Attach the token data to the request state
    request.state.token_data = token_data
    response = await call_next(request)
    return response

@app.get("/place/username")
async def protected_route(request: Request):
    token_data = request.state.token_data
    return {"message": f"Hello {token_data.username}"}

@app.get("/")
async def read_root():
    store_draw_info(cassandra_session, 1, 1, 1, "test", datetime.utcnow())
    
    return {"message": "Welcome to the Place API"}

@app.get("/api/place/delay")
def get_delay():
    return {"delay": DELAY}

@app.get("/api/place/board-bitmap")
def get_board_bitmap():
    return get_bitfield_as_integers(redis_session, key, BOARD_SIZE*BOARD_SIZE)


@app.get("/api/place/board-bitmap/pixel/")
async def get_pixel(x: int = Query(..., ge=0, lt=BOARD_SIZE), y: int = Query(..., ge=0, lt=BOARD_SIZE)):
    color = get_pixel_color(redis_session, key, x, y)
    return {"pixel_color": color}


@app.post("/api/place/draw")
async def draw_on_board(command: DrawCommand, request: Request):
    index = command.x + command.y * BOARD_SIZE
    if not (0 <= command.x <  BOARD_SIZE and  0 <= command.y <  BOARD_SIZE):
        raise HTTPException(status_code=400, detail="Coordinates out of bounds")
    if not (0 <= command.color <  MAX_COLORS):
        raise HTTPException(status_code=400, detail="Invalid color value")
    # Check if the delay has passed since the last draw
    last_timestamp = get_last_user_timestamp(cassandra_session, request.state.token_data.username)
    if last_timestamp:
        time_diff = (datetime.utcnow() - last_timestamp).total_seconds()
        if time_diff < DELAY:
            raise HTTPException(status_code=429, detail=f"Please wait {DELAY - time_diff:.2f} seconds before drawing again")
    
    ts = datetime.utcnow()
    # Update the last tile timestamp for the user
    set_last_user_timestamp(cassandra_session, request.state.token_data.username, ts)
    
    store_draw_info(cassandra_session, command.x, command.y, command.color, request.state.token_data.username, ts)
    # Set the pixel color in Redis
    set_4bit_value(redis_session, key, index, command.color)
    # Notify all WebSocket clients about the draw
    start_time = time.time()

    for connection in list(active_connections):
        try:
            await connection.send_json({
                "type": "draw",
                "x": command.x,
                "y": command.y,
                "color": command.color,
                "user": request.state.token_data.username,
                "timestamp": ts.isoformat()
            })
        except WebSocketDisconnect:
            active_connections.remove(connection)
    end_time = time.time()
    if ENABLE_TIMING:
        execution_time = (end_time - start_time) * 1000
        logger.info(f"{ts} Websocket send: {execution_time}ms, {len(active_connections)} users  (by {request.state.token_data.username})")
    return {"message": "Pixel updated successfully"}

@app.get("/api/place/last-user-timestamp/")
async def get_user_last_timestamp(request: Request):
    timestamp = get_last_user_timestamp(cassandra_session, request.state.token_data.username)
    if timestamp is None:
        raise HTTPException(status_code=404, detail="No timestamp found for user")
    return {"timestamp": timestamp}


@app.websocket("/api/place/board-bitmap/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Get the headers from the WebSocket connection request
    headers = dict(websocket._headers)
    # Convert header names to lowercase for case-insensitive comparison
    headers = {k.lower(): v for k, v in headers.items()}
    # Check for 'cookie' header
    cookies = headers.get('cookie')
    if not cookies:
        print("No cookies in websocket headers")
        await websocket.close(code=1008)
        return
    # Parse the cookies to find the token
    cookies = {cookie.split('=')[0]: cookie.split('=')[1] for cookie in cookies.split('; ')}
    token = cookies.get('token')
    if not token:
        print("No token in websocket headers")
        await websocket.close(code=1008)
        return
    try:
        token_data = decode_jwt(token)
    except Exception as e:
        print("Token error for websocket : ", e)
        await websocket.close(code=1008)
        return
    # If the token is valid, add the websocket to the active connections
    active_connections.add(websocket)
    # Attach the token data to the websocket
    websocket.token_data = token_data
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)
