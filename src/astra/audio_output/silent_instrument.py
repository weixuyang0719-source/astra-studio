"""Keep the user's joint intent while the unbound instrument bus is silent."""
class AudioOnlyJointOwner:
    def __init__(self,owner,mode):self.owner,self.mode=owner,mode
    def __getattr__(self,name):return getattr(self.owner,name)
    def apply(self,delta,revision):
        actual=dict(delta)
        if 'audition' in actual:actual['audition']='audio'
        if 'mode' in actual:actual['mode']=self.mode
        self.owner.apply(actual,revision)
    def snapshot(self):return self.owner.snapshot()
    def close(self):return self.owner.close()
