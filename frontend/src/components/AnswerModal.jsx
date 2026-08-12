import React, { useState } from 'react'

export default function AnswerModal({ onClose, onSubmit, strictMode }){
  const [title, setTitle] = useState('')
  const [artist, setArtist] = useState('')

  return (
    <div style={{position:'fixed',left:0,top:0,right:0,bottom:0,display:'flex',alignItems:'center',justifyContent:'center',background:'rgba(0,0,0,0.5)'}}>
      <div style={{background:'white',padding:28,borderRadius:12,width:640,maxWidth:'95%'}}>
        <h3 style={{fontSize:24,marginTop:0}}>Submit Answer</h3>
        <div style={{display:'flex',gap:12,marginBottom:12}}>
          <input placeholder='Song title' value={title} onChange={e=>setTitle(e.target.value)} style={{flex:1,padding:12,fontSize:18}} />
          {strictMode && <input placeholder='Artist' value={artist} onChange={e=>setArtist(e.target.value)} style={{flex:1,padding:12,fontSize:18}} />}
        </div>
        <div style={{display:'flex',justifyContent:'flex-end',gap:12}}>
          <button onClick={onClose} style={{padding:'10px 14px',fontSize:16}}>Cancel</button>
          <button onClick={()=>onSubmit({title,artist}, true)} style={{padding:'10px 14px',fontSize:16}}>Mark Correct</button>
          <button onClick={()=>onSubmit({title,artist}, false)} style={{padding:'10px 14px',fontSize:16}}>Mark Wrong</button>
        </div>
      </div>
    </div>
  )
}
