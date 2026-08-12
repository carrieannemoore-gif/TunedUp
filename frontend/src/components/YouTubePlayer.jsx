import React, { useEffect, useImperativeHandle, forwardRef, useRef } from 'react'

// Simple YouTube IFrame wrapper exposing play/pause

const YouTubePlayer = forwardRef(function YouTubePlayer({ videoId, start=0, end=null }, ref){
  const playerRef = useRef(null)
  const containerRef = useRef(null)

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
        height: '200',
        width: '360',
        videoId: videoId,
        playerVars: {
          start: start,
          end: end,
          controls: 1,
          modestbranding: 1,
          rel: 0
        },
        events: {
          onReady: (e) => {
            // auto-play snippet when ready
            // e.target.playVideo()
          },
          onStateChange: (e) => {
            // stop at end if needed
            if(end && e.data===window.YT.PlayerState.PLAYING){
              // poll for time and stop at end
              const check = setInterval(()=>{
                const t = e.target.getCurrentTime()
                if(t >= end - 0.25){
                  e.target.pauseVideo()
                  clearInterval(check)
                }
              }, 200)
            }
          }
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
    pause(){ if(playerRef.current) playerRef.current.pauseVideo() }
  }))

  return <div ref={containerRef}></div>
})

export default YouTubePlayer
