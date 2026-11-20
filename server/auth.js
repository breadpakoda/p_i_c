const jwt = require("jsonwebtoken");


const authenticationToken=(req,res,next)=>{
    const authHeader= req.headers.authorization;

    if(!authHeader){
        return res.status(401).json({
            message:"Token requierd"
        });

    }

    const token= authHeader.split(" ")[1];
    try{
        const secret = process.env.JWT_SECRET
        if (!secret) {
            throw new Error('JWT_SECRET is not configured')
        }
        const decoded=jwt.verify(token, secret);

        req.user=decoded;
        next();
    }
    catch(error){
        return res.status(401).json({
            message:"Invalid token"
        })
    }
}

module.exports = authenticationToken;