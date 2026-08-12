const express = require('express')
const path = require('path')
const fs = require('fs')
const cors = require('cors')
const app = express()
const PORT = process.env.PORT || 3000

app.use(cors())
app.use(express.json())

// serve sample songs
app.get('/api/songs', (req, res) => {
  res.sendFile(path.join(__dirname, '../frontend/public/sample_songs.json'))
})

const SESSIONS_FILE = path.join(__dirname, 'sessions.json')

app.post('/api/save-session', (req, res) => {
  const data = req.body || {}
  try{
    fs.writeFileSync(SESSIONS_FILE, JSON.stringify(data, null, 2))
    res.json({ok:true})
  }catch(err){
    console.error('save error',err)
    res.status(500).json({ok:false})
  }
})
app.get('/api/load-session', (req, res) => {
  if(fs.existsSync(SESSIONS_FILE)){
    const data = JSON.parse(fs.readFileSync(SESSIONS_FILE,'utf8'))
    res.json(data)
  } else res.status(404).json({ok:false})
})

// serve frontend build if present
app.use(express.static(path.join(__dirname, '../frontend/dist')))

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, '../frontend/dist/index.html'))
})

app.listen(PORT, ()=>{
  console.log('TunedUp backend server running on port', PORT)
})
