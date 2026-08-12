const express = require('express')
const path = require('path')
const app = express()
const PORT = process.env.PORT || 3000

app.use(express.json())

// serve sample songs
app.get('/api/songs', (req, res) => {
  res.sendFile(path.join(__dirname, '../frontend/public/sample_songs.json'))
})

// serve frontend dev static if built
app.use(express.static(path.join(__dirname, '../frontend/dist')))

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, '../frontend/dist/index.html'))
})

app.listen(PORT, ()=>{
  console.log('TunedUp backend server running on port', PORT)
})
