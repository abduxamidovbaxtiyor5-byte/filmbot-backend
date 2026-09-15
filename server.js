const express = require('express');
const http = http = require('http');
const { Server } = require('socket.io');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: {
        origin: "*",
        methods: ["GET", "POST"]
    }
});

io.on('connection', (socket) => {
    const { roomId, userName } = socket.handshake.query;

    if (roomId) {
        socket.join(roomId);
        console.log(`Пользователь ${userName} вошел в комнату: ${roomId}`);

        socket.on('chat_message', (data) => {
            io.to(data.roomId).emit('chat_message', {
                user: data.user,
                text: data.text
            });
        });

        socket.on('disconnect', () => {
            console.log(`Пользователь покинул комнату: ${roomId}`);
        });
    }
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
    console.log(`Сервер запущен на порту ${PORT}`);
});