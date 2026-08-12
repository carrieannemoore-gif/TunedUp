import React, { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

// Enhanced YouTube player with seek and timed play
const YouTubePlayer = forwardRef(function YouTubePlayer({ videoId, start=0, end=null }, ref){
  const playerRef = useRef(null)
  const containerRef = useRef(null)
  const timerRef = useRef(null)

  useEffect(()=>{
    // load API if not present
    if(!window.YT){
      const tag = document.createElement('script')
      tag.src = 'https://www.youtube.com/iframe_api'
      document.body.appendChild(tag)
    }

    let mounted = true
    function create(){
      if(!mounted) return
      playerRef.current = new window.YT.Player(containerRef.current, {
        height: '240',
        width: '480',
        videoId: videoId,
        playerVars: {
          start: start,
          end: end,
          controls: 1,
          modestbranding: 1,
          rel: 0
        }
      })
    }

    if(window.YT && window.YT.Player){
      create()
    } else {
      window.onYouTubeIframeAPIReady = create
    }

    return ()=>{ mounted = false }
  }, [videoId, start, end])

  useImperativeHandle(ref, ()=>({
    play(){ if(playerRef.current) playerRef.current.playVideo() },
    pause(){ if(playerRef.current) playerRef.current.pauseVideo() },
    seekTo(sec){ if(playerRef.current) playerRef.current.seekTo(sec, true) },
    // play for N seconds from current playhead (or start)
    playFor(seconds){
      if(!playerRef.current) return
      const p = playerRef.current
      const startAt = p.getCurrentTime ? p.getCurrentTime() : 0
      p.seekTo(startAt, true)
      p.playVideo()
      if(timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(()=>{ p.pauseVideo() }, seconds*1000)
    }
  }))

  return <div ref={containerRef}></div>
})

export default YouTubePlayer
